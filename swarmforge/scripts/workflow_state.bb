#!/usr/bin/env bb
(ns workflow-state
  (:require [babashka.fs :as fs]
            [clojure.edn :as edn]
            [clojure.string :as str]))

(defn sha256 [text]
  (let [bytes (.digest (java.security.MessageDigest/getInstance "SHA-256")
                       (.getBytes (str text) "UTF-8"))]
    (apply str (map #(format "%02x" (bit-and % 255)) bytes))))

(defn headers [file]
  (into {} (keep (fn [line]
                   (let [[k v] (str/split line #": " 2)] (when v [k v])))
                 (take-while (complement str/blank?)
                             (str/split-lines (slurp (str file)))))))

(defn files [dir pattern]
  (if (fs/directory? dir)
    (vec (filter fs/regular-file?
                 (distinct (concat (fs/glob dir pattern)
                                   (when (str/starts-with? pattern "**/")
                                     (fs/glob dir (subs pattern 3)))))))
    []))

(defn forwarding-required? [file]
  (let [h (headers file)]
    (and (not= "true" (get h "non-forwarding"))
         (or (= "git_handoff" (get h "type"))
             (#{"(New Task)" "(Retry)"} (get h "from"))))))

(defn outbound [root worktree stages]
  (for [base (distinct [(fs/path root ".swarmforge/handoffs")
                       (fs/path worktree ".swarmforge/handoffs")])
        stage stages
        path (files (fs/path base stage) "*.handoff")]
    {:path (str path) :stage stage :headers (headers path)}))

(defn forward? [h]
  (and (= "git_handoff" (get h "type"))
       (not= "reverse" (get h "handoff_kind"))
       ;; Legacy reverse copies have no explicit kind, but are non-forwarding 00.
       (not (and (nil? (get h "handoff_kind"))
                 (= "true" (get h "non-forwarding")) (= "00" (get h "priority"))))))

(defn fingerprints [h]
  (set (remove str/blank? (str/split (get h "input_fingerprints" "") #","))))

(defn matching-forward [root role worktree file stages]
  (let [fingerprint (sha256 (slurp (str file)))]
    (first (filter #(and (= role (get-in % [:headers "from"]))
                         (forward? (:headers %))
                         (contains? (fingerprints (:headers %)) fingerprint))
                   (outbound root worktree stages)))))

(defn publication-blocker [root role]
  (some (fn [file]
          (let [receipt (try (edn/read-string (slurp (str file))) (catch Exception _ nil))]
            (when-not (#{:complete :completing} (:phase receipt))
              {:code "AUDIT_PUBLICATION_PENDING" :owner "runtime"
               :message "Pubblicazione da riprendere — Runtime."
               :next_action "Retry swarm_audit.sh complete with the original result JSON. The runtime resumes the persisted publication without generating another handoff."
               :evidence (str file)})))
        (files (fs/path root ".swarmforge/handoffs/audit_completed" (sha256 role)) "*.edn")))

(defn audit-blocker [root role]
  (or (publication-blocker root role)
  (when-let [file (first (files (fs/path root ".swarmforge/handoffs/audit_pending" (sha256 role)) "*.edn"))]
    (let [candidate (try (:candidate (edn/read-string (slurp (str file)))) (catch Exception _ nil))]
      {:code (if (and (map? candidate) (= role (:sender candidate))) "AUDIT_PENDING" "AUDIT_STATE_INVALID")
       :owner role
       :message (str "Audit da completare — " role ". Handoff non pubblicato.")
       :next_action "Review the candidate and run swarm_audit.sh complete ./tmp/audit-result.json; the runtime publishes the handoff. Use swarm_audit.sh status DRAFT for its result template. If unreadable, report the state file; do not delete it to bypass review."
       :evidence (str file)}))))

(defn clarification-blocker [root role]
  (when-let [file (first (filter #(= role (get (headers %) "role"))
                                 (files (fs/path root ".swarmforge/dashboard/clarifications/pending") "*.request")))]
    {:code "CLARIFICATION_PENDING"
     :message "An operator clarification is pending or its answer has not been delivered."
     :next_action "Answer or retry the clarification from Attention after the agent is ready."
     :evidence (str file)}))

(defn completion-blocker [root role worktree inputs]
  (or (audit-blocker root role)
      (clarification-blocker root role)
      (some (fn [file]
              (when (and (forwarding-required? file)
                         (nil? (matching-forward root role worktree file ["outbox" "pending_approval" "sent"])))
                (if-let [failed (matching-forward root role worktree file ["failed"])]
                  {:code "HANDOFF_FAILED" :message "The required forward handoff failed; current work remains open."
                   :next_action "Read the failed handoff and its .error file, resolve the delivery problem and resubmit through the helper."
                   :evidence (:path failed)}
                  {:code "HANDOFF_REQUIRED" :message "Current work has no published forward handoff."
                   :next_action "Complete the assigned work and queue a git_handoff with swarm_handoff.sh."
                   :evidence (str file)}))) inputs)))

(defn task-match? [task h]
  (if-let [id (not-empty (get h "task_id"))]
    (= (:id task) id)
    (= (:name task) (get h "task"))))

(defn failed-unresolved? [entry published]
  (let [h (:headers entry)]
    (not-any? (fn [other]
                (let [o (:headers other)]
                  (and (= (get h "from") (get o "from"))
                       (or (and (not-empty (get h "id")) (= (get h "id") (get o "id")))
                           (and (forward? h) (forward? o) (seq (fingerprints h))
                                (= (fingerprints h) (fingerprints o))))))) published)))

(defn task-state [root rows task]
  (let [row (some #(when (= (:lane task) (first %)) %) rows)
        role (first row)
        worktree (nth row 2 root)
        entries (distinct (mapcat #(outbound root (nth % 2 root)
                                              ["outbox" "pending_approval" "sent" "failed"]) rows))
        relevant (filter #(task-match? task (:headers %)) entries)
        published (remove #(= "failed" (:stage %)) relevant)
        failed (first (filter #(and (= "failed" (:stage %))
                                    (failed-unresolved? % published)) relevant))
        audit (some (fn [r]
                      (when-let [block (audit-blocker root (first r))]
                        (let [candidate (try (:candidate (edn/read-string (slurp (:evidence block))))
                                             (catch Exception _ nil))]
                          (when (or (= (:id task) (:task-id candidate))
                                    (and (= role (first r)) (nil? candidate)))
                            (assoc block :role (first r)))))) rows)
        clar (when role (clarification-blocker root role))
        current (mapcat (fn [r]
                          (for [file (files (fs/path (nth r 2 root) ".swarmforge/handoffs/inbox/in_process") "**/*.handoff")
                                :when (task-match? task (headers file))]
                            {:file file :role (first r) :worktree (nth r 2 root)})) rows)
        queued (some #(when (and (= "pending_approval" (:stage %)) (forward? (:headers %))) %) relevant)
        outbound-queued (some #(when (= "outbox" (:stage %)) %) relevant)
        new-input (some (fn [r]
                          (when-let [file (first (filter #(task-match? task (headers %))
                                                       (files (fs/path (nth r 2 root) ".swarmforge/handoffs/inbox/new") "**/*.handoff")))]
                            {:file file :role (first r)})) rows)
        terminal-role (first (last rows))
        terminal-sent (some #(when (and (= "sent" (:stage %))
                                        (= terminal-role (get-in % [:headers "from"]))
                                        (forward? (:headers %))) %) relevant)
        base {:task (:name task) :task_id (:id task) :role role}
        status (fn [state message action evidence]
                 (merge base {:state state :message message :next_action action :evidence evidence}))]
    (cond
      audit (merge (status "audit-pending" (:message audit) (:next_action audit) (:evidence audit)) audit)
      clar (merge (status "awaiting-clarification" (:message clar) (:next_action clar) (:evidence clar)) clar)
      failed (assoc (status "handoff-failed" "A handoff failed delivery."
                           "Inspect the failed handoff and its .error file; restore delivery before declaring the workflow complete." (:path failed))
                    :code "HANDOFF_FAILED" :role (get-in failed [:headers "from"]))
      queued (assoc (status "awaiting-approval" "Handoff queued; operator approval is pending."
                            "Review the proposed artifacts and approve or request changes in Attention." (:path queued))
                    :role (get-in queued [:headers "from"]))
      outbound-queued (assoc (status "handoff-queued" "Handoff published; delivery has not completed."
                                     "Wait for the daemon; inspect its log if the queue does not advance." (:path outbound-queued))
                             :role (get-in outbound-queued [:headers "from"]))
      (seq current) (let [item (first current)
                          inputs (map :file (filter #(= (:role item) (:role %)) current))
                          problem (completion-blocker root (:role item) (:worktree item) inputs)
                          verifying? (every? #(not (forwarding-required? %)) inputs)]
                      (merge
                       (assoc (status (case (:code problem)
                                        ("AUDIT_PENDING" "AUDIT_STATE_INVALID") "audit-pending"
                                        "CLARIFICATION_PENDING" "awaiting-clarification"
                                        "HANDOFF_FAILED" "handoff-failed"
                                        (if verifying? "verifying" "working"))
                                      "An agent has current work."
                                      (if verifying?
                                        "Verify the received work and run done_with_current.sh; no downstream handoff is required for these inputs."
                                        "Finish the role work, perform the audit and publish the required handoff.")
                                      (str (:file item))) :role (:role item))
                       problem))
      new-input (assoc (status "queued" "Work is waiting in an agent inbox." "The assigned agent must run ready_for_next.sh." (str (:file new-input)))
                       :role (:role new-input))
      (and (= "done" (:lane task)) terminal-sent)
      (status "done" "Terminal handoff delivered; no pending work found for this task."
              "Review the application results and any explicitly unperformed manual checks." (:path terminal-sent))
      (= "done" (:lane task))
      (assoc (status "unverified-completion" "The board says Done, but terminal delivery evidence is missing."
                     "Inspect the last role and daemon history; do not infer completion from a board label alone." nil)
             :code "DONE_UNVERIFIED" :role terminal-role)
      (= "rejected" (:lane task)) (status "rejected" "Operator requested changes."
                                          "Review the operator findings before retrying." nil)
      (nil? row) (assoc (status "invalid-role" "The board lane does not name a configured role."
                               "Inspect the project configuration and board state." nil)
                       :code "WORKFLOW_ROLE_UNKNOWN")
      :else (assoc (status "missing-work" "Card assigned, but no queued or current work exists."
                           "Resume from Attention if the completed input can be restored unambiguously." nil)
                   :code "MISSING_CURRENT_WORK"))))
