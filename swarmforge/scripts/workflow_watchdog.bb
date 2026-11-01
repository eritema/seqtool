#!/usr/bin/env bb
(ns workflow-watchdog
  (:require [babashka.fs :as fs]
            [clojure.edn :as edn]
            [clojure.java.shell :refer [sh]]
            [clojure.string :as str]))

(load-file (str (fs/path (fs/parent *file*) "swarm_startup.bb")))
(load-file (str (fs/path (fs/parent *file*) "agent_lifecycle.bb")))

(def quiet-ms 30000)
(def retry-ms 60000)
(def max-attempts 3)

(defn recovery-control-file [root role]
  (fs/path root ".swarmforge/agents/recovery-control" (str role ".edn")))

(defn recovery-paused? [root role]
  (let [file (recovery-control-file root role)]
    (and (fs/exists? file)
         ;; An unreadable control must not authorize automatic terminal input.
         (try (not= false (:paused (edn/read-string (slurp (str file)))))
              (catch Exception _ true)))))

(defn state-file [root role]
  (fs/path root ".swarmforge/daemon/workflow-watchdog" (str role ".edn")))

(defn read-state [root role]
  (let [file (state-file root role)]
    (when (fs/exists? file)
      (edn/read-string (slurp (str file))))))

(defn write-state! [root role state]
  (let [file (state-file root role)]
    (fs/create-dirs (fs/parent file))
    (let [tmp (fs/create-temp-file {:dir (fs/parent file) :prefix ".watchdog-"})]
      (spit (str tmp) (pr-str state))
      (fs/move tmp file {:replace-existing true}))))

(defn headers [file]
  (into {} (keep (fn [line]
                  (let [[k v] (str/split line #": " 2)]
                    (when v [k v])))
                (str/split-lines (first (str/split (slurp (str file)) #"\n\n" 2))))))

(defn files [dir pattern]
  (if (fs/directory? dir)
    (sort-by str (distinct (concat (fs/glob dir pattern)
                                  (when (str/starts-with? pattern "**/")
                                    (fs/glob dir (subs pattern 3))))))
    []))

(defn pending-work [worktree]
  (let [current (files (fs/path worktree ".swarmforge/handoffs/inbox/in_process") "**/*.handoff")]
    ;; Additional queued tasks must not reset the current task's retry budget.
    (vec (if (seq current) current
             (take 1 (files (fs/path worktree ".swarmforge/handoffs/inbox/new") "*.handoff"))))))

(defn gate [root role worktree]
  (cond
    (some #(= role (get (headers %) "from"))
          (concat (files (fs/path root ".swarmforge/handoffs/pending_approval") "*.handoff")
                  (files (fs/path worktree ".swarmforge/handoffs/outbox") "*.handoff")))
    :outbound
    (some #(= role (get (headers %) "role"))
          (files (fs/path root ".swarmforge/dashboard/clarifications/pending") "*.request"))
    :clarification
    (some #(= role (:sender (:candidate (edn/read-string (slurp (str %))))))
          (files (fs/path root ".swarmforge/handoffs/audit_pending") "**/*.edn"))
    :audit))

(defn codex-command? [command]
  (boolean (re-matches #"codex(?:[-.].*)?" (or command ""))))

(defn foreground-codex [pane-pid process-table]
  (let [rows (keep (fn [line]
                     (when-let [[_ pid parent group foreground command]
                                (re-matches #"\s*(\d+)\s+(\d+)\s+(-?\d+)\s+(-?\d+)\s+(.+?)\s*" line)]
                       {:pid (parse-long pid) :parent (parse-long parent)
                        :group (parse-long group) :foreground (parse-long foreground)
                        :command (fs/file-name command)}))
                   (str/split-lines (or process-table "")))
        by-pid (into {} (map (juxt :pid identity) rows))
        foreground (:foreground (get by-pid pane-pid))
        descendant? (fn [pid]
                      (loop [pid pid seen #{}]
                        (cond
                          (= pid pane-pid) true
                          (or (nil? pid) (contains? seen pid)) false
                          :else (recur (:parent (get by-pid pid)) (conj seen pid)))))]
    (when (and foreground (pos? foreground))
      (first (sort-by :pid (filter #(and (= foreground (:group %))
                                        (codex-command? (:command %))
                                        (descendant? (:pid %))) rows))))))

(defn empty-input-below-prompt? [lines {:keys [x y]}]
  ;; Codex's no-alt-screen layout can put the cursor on the blank row below
  ;; the marker. Accept only the observed empty two-row input above its footer.
  (when (and x y (pos? y) (= (count lines) (+ y 3)))
    (let [prompt (get lines (dec y))
          marker (str/index-of prompt "›")]
      (and marker
           (= "›" (str/trim prompt))
           (<= (inc marker) x (+ marker 2))
           (str/blank? (get lines y))
           (str/blank? (get lines (inc y)))
           (re-matches #"\s{2,}\S.* · .+" (get lines (+ y 2)))))))

(defn pane-state [agent command text & [cursor]]
  ;; Fail closed: only a recognized Codex process with an empty input prompt
  ;; can receive an automatic reminder. Silence alone is never sufficient.
  (let [lines (vec (str/split-lines (or text "")))
        ;; Codex leaves the previous answer visible above its completed-turn
        ;; marker. Words such as "approval" in that answer are history, not a
        ;; live approval dialog. A new dialog or activity indicator below the
        ;; marker must still block delivery, even with an empty prompt visible.
        completed-line (last (keep-indexed (fn [i line]
                                            (when (re-matches #"\s*Worked for .+" line) i)) lines))
        active-lines (if completed-line (subvec lines (inc completed-line)) lines)
        ;; A pasted multiline answer can push the input marker above the last
        ;; twelve rows. It remains a draft, never an empty/idle input.
        prompt (last (filter #(str/starts-with? % "›") (map str/trim active-lines)))
        recent (->> active-lines (take-last 12) (map str/trim)
                    (remove str/blank?) (str/join "\n"))
        input-line (when cursor (get lines (:y cursor)))
        marker (when input-line (str/index-of input-line "›"))
        empty-input? (and marker
                          (str/blank? (subs input-line 0 marker))
                          (<= (inc marker) (:x cursor) (+ marker 2)))]
    (cond
      (or (str/blank? text) (not= agent "codex")
          (not (codex-command? command))) :unknown
      (re-find #"(?i)esc.*interrupt|interrupt.*esc|working|thinking|running|connecting|reconnecting" recent) :busy
      (re-find #"(?i)approve|approval|allow this|would you like|yes.*no|press enter to|sign in|log in" recent) :waiting-user
      ;; Cursor position distinguishes Codex's placeholder from typed input.
      (or empty-input? (empty-input-below-prompt? lines cursor)) :idle
      (and (nil? cursor) (= "›" prompt)) :idle
      prompt :draft
      :else :unknown)))

(defn observe-pane [socket session]
  (let [info (sh "tmux" "-S" socket "display-message" "-p" "-t" session
                 ;; tmux can sanitize literal tabs into underscores. Use a
                 ;; printable separator and reject malformed metadata as a unit.
                 "#{pane_current_command}|#{cursor_x}|#{cursor_y}|#{pane_pid}|#{pane_id}|#{pane_dead}")
        pane (sh "tmux" "-S" socket "capture-pane" "-p" "-t" session)
        [_ command x y pid pane-id dead]
        (when (zero? (:exit info))
          (re-matches #"([^|\r\n]*)\|([0-9]+)\|([0-9]+)\|([0-9]+)\|(%[0-9]+)\|([01])"
                      (str/trim (or (:out info) ""))))
        ;; tmux can report the shell wrapper even while Codex owns the input.
        ;; Verify ancestry AND the foreground process group; a background Codex
        ;; process or old screen output must never authorize shell input.
        codex (when (and (not (codex-command? command)) (re-matches #"\d+" (or pid "")))
                (let [result (sh "ps" "-ax" "-o" "pid=,ppid=,pgid=,tpgid=,comm=")]
                  (when (zero? (:exit result))
                    (foreground-codex (parse-long pid) (:out result)))))]
    (cond-> {:command (if codex (:command codex) command)
     :cursor (when (and (re-matches #"\d+" (or x "")) (re-matches #"\d+" (or y "")))
               {:x (parse-long x) :y (parse-long y)})
     :text (when (zero? (:exit pane)) (:out pane))}
      pane-id (assoc :pane-id pane-id)
      pid (assoc :pane-pid pid)
      (= dead "1") (assoc :dead? true)
      codex (assoc :reported-command command :codex-pid (:pid codex)))))

(defn next-state [previous {:keys [identity work-key progress pane status gate now paused?]}]
  (let [same-work? (if (and work-key (:work-key previous))
                     (= work-key (:work-key previous))
                     (= identity (:identity previous)))
        previous (if same-work? previous
                    {:identity identity :attempts 0 :changed-at now})
        changed? (not= pane (:pane previous))
        state (cond-> (assoc previous :identity identity :pane pane :changed-at (if changed? now (:changed-at previous))
                     :status status :gate gate :action nil :recovery-paused (boolean paused?))
                work-key (assoc :work-key work-key)
                progress (assoc :progress progress :progress-at
                                (if (= progress (:progress previous)) (:progress-at previous) now)))
        quiet? (>= (- now (:changed-at state)) quiet-ms)
        ready? (>= (- now (or (:last-attempt state) (:changed-at state))) retry-ms)]
    (cond
      paused? (assoc state :code "AGENT_RECOVERY_PAUSED")
      ;; An audit is work for the agent, not an operator approval gate.
      (and gate (not= :audit gate)) (assoc state :code nil)
      (= status :exited) (assoc state :code "AGENT_EXITED")
      (:stalled state) (assoc state :code "AGENT_STALLED")
      (= status :busy) (assoc state :code nil)
      (= status :waiting-user) (assoc state :code "AGENT_WAITING_FOR_USER")
      ;; A finished response changes the pane, but cannot hide exhausted retries.
      (and (= status :idle) (>= (:attempts state) max-attempts))
      (cond-> (assoc state :code (if ready? "AGENT_STALLED" "AGENT_RECOVERY"))
        ready? (assoc :stalled true))
      (not quiet?) (assoc state :code nil)
      (not= status :idle) (assoc state :code "AGENT_STATE_UNCONFIRMED")
      ready? (assoc state :action :remind :code "AGENT_RECOVERY" :last-attempt now
                    :attempts (inc (:attempts state)))
      :else (assoc state :code "AGENT_RECOVERY"))))

(defn reminder [current? & [gate]]
  (cond
    (= :audit gate)
    "Your handoff is not queued: an audit is pending. AUDIT_REQUIRED requests your review; it is not a failed analysis-tool result. Read the audit candidate, complete inbound requirements and project-specific overrides in project.prompt. Review the committed work, fix and commit any findings, and run only the applicable checks. Then record the structured review with swarm_audit.sh complete ./tmp/audit-result.json. Use swarm_audit.sh status DRAFT for the current candidate template. The runtime publishes it; do not wait for an external audit decision or repeat swarm_handoff.sh to publish. Inspect the actual helper result. Do not infer a tool failure without a command result. Do not bypass the audit or mark unfinished work complete. If blocked, write the concrete blocker to ./tmp/question.txt and run pack_dashboard_request.sh clarify ./tmp/question.txt."
    current?
    "Your current task is still open and no outbound handoff is queued. Continue the assigned work, run the required checks, and submit the downstream handoff when ready. If blocked, report the concrete blocker. Do not mark unfinished work complete."
    :else
    "You have assigned work waiting in your inbox. Execute ready_for_next.sh now, read the received task, and carry out the role workflow. Continue beyond summarizing instructions; submit the required handoff when ready or report a concrete blocker."))

(def input-settle-ms 1000)

(defn settle-input! []
  (Thread/sleep input-settle-ms))

(defn send-reminder! [socket session text & [before-submit]]
  ;; Allow the TUI to finish processing the pasted burst before Enter.
  ;; Never send an extra C-j into the next turn.
  (doseq [keys [["-l" text] ["C-m"]]]
    (when (= keys ["C-m"])
      (settle-input!)
      (when before-submit (before-submit)))
    (let [result (apply sh (concat ["tmux" "-S" socket "send-keys" "-t" session] keys))]
      (when-not (zero? (:exit result))
        (throw (ex-info "Cannot deliver workflow reminder" result))))))

(defn same-process? [before after]
  (and (not (:dead? after))
       (= (:command before) (:command after))
       (= (:codex-pid before) (:codex-pid after))
       (= (:pane-id before) (:pane-id after))
       (= (:pane-pid before) (:pane-pid after))))

(defn deliver-input! [root role agent socket target text send! & [{:keys [automatic? expected-generation session-observer]}]]
  (agent-lifecycle/with-role-lock! root role
    (fn []
      (when (and expected-generation
                 (not= expected-generation (:generation (agent-lifecycle/read-state root role))))
        (throw (ex-info "Agent session changed since selection; refresh before sending."
                        {:http-status 409 :code "AGENT_GENERATION_CHANGED"})))
      (when (and automatic? (recovery-paused? root role))
        (throw (ex-info "Automatic notification deferred: role recovery is paused."
                        {:http-status 409 :code "AGENT_RECOVERY_PAUSED"})))
      (when (#{:failed :starting} (:status (swarm-startup/read-state root role)))
        (throw (ex-info "Agent startup is not ready; message was not sent." {:http-status 409})))
      (let [pane (observe-pane socket target)
            ready? (if (= agent "codex")
                     (and (not (:dead? pane)) (codex-command? (:command pane))
                          (= :idle (pane-state agent (:command pane) (:text pane) (:cursor pane))))
                     (and (not (:dead? pane)) (#{"claude" "copilot" "grok"} agent)
                          (= agent (:command pane))))]
        (when-not ready?
          (throw (ex-info "Message not sent: the expected agent is not confirmed ready. Open its terminal."
                          {:http-status 409})))
        (when session-observer
          (session-observer {:session (select-keys pane [:pane-id :pane-pid :codex-pid :command])
                             :generation (:generation (agent-lifecycle/read-state root role))}))
        (send! socket target text
               (fn []
                 (let [latest (observe-pane socket target)]
                   (when-not (and (or (not automatic?) (not (recovery-paused? root role)))
                                  (same-process? pane latest)
                                  (or (not= agent "codex")
                                      (#{:idle :draft} (pane-state agent (:command latest) (:text latest) (:cursor latest)))))
                     (throw (ex-info "Agent changed during delivery. Enter was not sent; inspect its terminal."
                                     {:http-status 409}))))))))))

(defn check-role-unlocked! [root socket {:keys [role worktree-path session agent]}]
  (let [work (pending-work worktree-path)
        old (read-state root role)]
    (if (empty? work)
      ;; Directory snapshots can miss an input while it moves between queues.
      ;; Keep the last budget; only a different logical task or explicit reset
      ;; can grant more attempts. No pending input means no issue or reminder.
      (when old
        (let [state (assoc old :action nil :code nil :status :no-work)]
          (when (not= old state) (write-state! root role state))))
      (let [identity (mapv str work)
            pane (observe-pane socket session)
            current-gate (cond
                           (#{:failed :starting} (:status (swarm-startup/read-state root role))) :startup
                           :else (gate root role worktree-path))
            state (next-state old {:identity identity
                                   :work-key (mapv (fn [file] (let [h (headers file)]
                                                               (or (get h "task_id") (get h "id") (get h "task") (str file)))) work)
                                   :progress {:phase (if (some #(str/includes? (str %) "/in_process/") work) :accepted :queued)
                                              :gate current-gate}
                                   :pane (hash pane)
                                   :status (if (:dead? pane) :exited (pane-state agent (:command pane) (:text pane) (:cursor pane)))
                                   :gate current-gate
                                   :now (System/currentTimeMillis)
                                   :paused? (recovery-paused? root role)})
            state (assoc state :tasks (mapv #(select-keys (headers %) ["task" "task_id"]) work))]
        ;; Persist the budget before injecting so a daemon restart cannot loop.
        (when (not= state old) (write-state! root role state))
        (when (and (= :remind (:action state))
                   (not= :failed (:status (swarm-startup/read-state root role))))
          (try
            ;; Recheck both work and the input immediately before sending.
            (let [latest (observe-pane socket session)]
              (if (and (not (recovery-paused? root role))
                       (= pane latest) (= identity (mapv str (pending-work worktree-path)))
                       (= (:gate state) (gate root role worktree-path)))
                (send-reminder! socket session
                                (reminder (some #(str/includes? (str %) "/in_process/") work)
                                          (:gate state))
                                (fn []
                                  ;; The paste-settle pause opens another race:
                                  ;; do not press Enter after Codex exits or a gate changes.
                                  (let [latest (observe-pane socket session)]
                                    (when-not
                                     (and (not (recovery-paused? root role))
                                          (codex-command? (:command latest))
                                          (same-process? pane latest)
                                          (not (#{:busy :waiting-user :unknown}
                                                (pane-state agent (:command latest) (:text latest) (:cursor latest))))
                                          (= identity (mapv str (pending-work worktree-path)))
                                          (= (:gate state) (gate root role worktree-path)))
                                      (throw (ex-info "Workflow state changed before reminder submission; Enter was not sent" {}))))))
                (write-state! root role (assoc state :action nil :code "AGENT_STATE_UNCONFIRMED"))))
            (catch Exception e
              (write-state! root role (assoc state :action nil :code "AGENT_RECOVERY_FAILED"
                                            :error (.getMessage e))))))))))

(defn check-role! [root socket role-info]
  (try
    (agent-lifecycle/with-role-lock! root (:role role-info)
      #(check-role-unlocked! root socket role-info))
    (catch clojure.lang.ExceptionInfo e
      (when-not (= "AGENT_COORDINATION_BUSY" (:code (ex-data e))) (throw e)))))

(def issue-messages
  {"AGENT_RECOVERY_PAUSED" "Operator paused automatic reminders and handoff wake-ups for this role. Work remains queued; resume recovery explicitly when ready."
   "AGENT_STALLED" "Automatic recovery exhausted its 3 attempts without completing this work. The block persists across output and agent restarts. Inspect the terminal or change model; reset recovery explicitly when ready."
   "AGENT_EXITED" "Agent exited with pending work. Inspect its terminal and restart only this role. Other roles are not stopped."
   "AGENT_RECOVERY" "Agent idle with pending work. Automatic recovery is limited to 3 reminders."
   "AGENT_WAITING_FOR_USER" "Agent is waiting for user input. Open its terminal; automatic recovery is paused."
   "AGENT_STATE_UNCONFIRMED" "Pending work exists, but agent readiness cannot be confirmed. Inspect its terminal, including any unsent input; automatic recovery is paused."
   "AGENT_RECOVERY_FAILED" "Automatic recovery failed. Inspect the agent terminal and daemon log."})

(def audit-issue-messages
  {"AGENT_STALLED" "Agent stopped with an audit pending after 3 reminders. Open its terminal, resolve the blocker, then reset recovery. Audit and operator approval remain required."
   "AGENT_RECOVERY" "Agent idle with an audit pending. Automatic audit recovery is limited to 3 reminders."})

(defn issues [root]
  (let [roles-file (fs/path root ".swarmforge/roles.tsv")
        worktrees (when (fs/exists? roles-file)
                    (into {} (map (fn [line]
                                    (let [[role _ wt] (str/split line #"\t")] [role wt]))
                                  (str/split-lines (slurp (str roles-file))))))]
    (vec
     (mapcat
      (fn [file]
        (let [role (str/replace (fs/file-name file) #"\.edn$" "")
              worktree (get worktrees role)
              work (when worktree (pending-work worktree))
              state (try (edn/read-string (slurp (str file)))
                         (catch Exception _ {:code "AGENT_RECOVERY_FAILED"}))]
          (when (and (:code state) (seq work)
                     (or (nil? (:identity state)) (= (:identity state) (mapv str work))))
            (map (fn [file]
                   (let [task (headers file)]
                     {:role role :task (get task "task") :task_id (get task "task_id")
                      :code (:code state)
                      :message (or (when (= :audit (:gate state))
                                     (get audit-issue-messages (:code state)))
                                   (get issue-messages (:code state)))})) work))))
      (files (fs/path root ".swarmforge/daemon/workflow-watchdog") "*.edn")))))

(defn require-configured-role! [root role]
  (when-not (and root role
                   (re-matches #"[A-Za-z0-9-]+" role)
                   (fs/regular-file? (fs/path root ".swarmforge/roles.tsv"))
                   (some #(= role (first (str/split % #"\t")))
                         (str/split-lines (slurp (str (fs/path root ".swarmforge/roles.tsv"))))))
    (throw (ex-info "Recovery control requires a configured role in an existing swarm." {}))))

(defn set-recovery-paused! [root role paused?]
  (require-configured-role! root role)
  (agent-lifecycle/with-role-lock! root role
    (fn []
      (let [file (recovery-control-file root role)]
        (fs/create-dirs (fs/parent file))
        (let [tmp (fs/create-temp-file {:dir (fs/parent file) :prefix ".recovery-"})]
          (spit (str tmp) (pr-str {:paused (boolean paused?)
                                  :changed-at (System/currentTimeMillis)}))
          (fs/move tmp file {:replace-existing true})))))
  (println (if paused? "RECOVERY_PAUSED:" "RECOVERY_RESUMED:") role
           "Task files and recovery budget were retained. Running turns were not interrupted."))

(defn reset-recovery! [root role]
  (require-configured-role! root role)
  (agent-lifecycle/with-role-lock! root role
    (fn []
      (let [file (state-file root role)
            history (fs/path root ".swarmforge/agents/history")]
        (when (fs/exists? file)
          (fs/create-dirs history)
          (fs/copy file (fs/path history (str role "-recovery-reset-" (java.util.UUID/randomUUID) ".edn")))
          (fs/delete file)))))
  (println "Recovery reset for" role "Audit, approval and task files were retained."))

(when (= (str *file*) (System/getProperty "babashka.file"))
  (let [[action root role :as args] *command-line-args*]
    (cond
      (and (= 3 (count args)) (#{"--pause" "--resume"} action))
      (set-recovery-paused! root role (= action "--pause"))
      (= 2 (count args)) (reset-recovery! action root)
      :else (throw (ex-info "Usage: workflow_watchdog.bb ROOT ROLE | --pause|--resume ROOT ROLE" {})))))
