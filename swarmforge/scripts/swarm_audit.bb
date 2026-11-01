#!/usr/bin/env bb
(ns swarm-audit
  (:require [babashka.fs :as fs]
            [cheshire.core :as json]
            [clojure.edn :as edn]
            [clojure.string :as str]))

(load-file (str (fs/path (fs/parent *file*) "swarm_handoff.bb")))

(defn fail! [code message]
  (throw (ex-info (str code ": " message) {:exit 2})))

(defn nonblank? [x] (and (string? x) (not (str/blank? x))))

(defn atomic-edn! [path data]
  (fs/create-dirs (fs/parent path))
  (let [tmp (fs/create-temp-file {:dir (fs/parent path) :prefix ".audit-receipt-"})]
    (spit (str tmp) (pr-str data))
    (fs/move tmp path {:replace-existing true})))

(defn receipt-file [sender audit-id]
  (fs/path (swarm-handoff/state-dir) "audit_completed" (swarm-handoff/sha256 sender)
           (str audit-id ".edn")))

(defn read-result! [path]
  (swarm-handoff/require-worktree-tmp-draft! path)
  (let [result (json/parse-string (slurp (str path)) true)
        keys-required #{:version :audit_id :candidate_commit :draft :decision
                        :requirements :checks :findings}
        evidence? (fn [x] (and (map? x) (nonblank? (:evidence x))))]
    (when-not (and (map? result) (= keys-required (set (keys result)))
                   (= 1 (:version result))
                   (string? (:audit_id result))
                   (re-matches #"[a-f0-9]{64}" (:audit_id result))
                   (nonblank? (:candidate_commit result)) (nonblank? (:draft result))
                   (= "complete" (:decision result))
                   (vector? (:requirements result)) (seq (:requirements result))
                   (every? #(and (evidence? %) (nonblank? (:requirement %))
                                 (#{"verified" "not_run"} (:status %))) (:requirements result))
                   (vector? (:checks result)) (seq (:checks result))
                   (every? #(and (evidence? %) (nonblank? (:command %))
                                 (#{"passed" "not_run"} (:status %))) (:checks result))
                   (vector? (:findings result)) (empty? (:findings result)))
      (fail! "AUDIT_RESULT_INVALID"
             "Expected version 1, decision complete, candidate identity, nonempty requirement/check evidence and no unresolved findings. not_run needs a reason in evidence."))
    result))

(defn print-publication! [receipt replay?]
  (println (if replay? "AUDIT_ALREADY_COMPLETED:" "AUDIT_COMPLETED:") (:audit-id receipt))
  (doseq [path (:outbound receipt)] (println "HANDOFF QUEUED:" path)))

(defn finish-publication! [path receipt]
  ;; The durable plan is written before any file becomes visible to handoffd.
  ;; A moved staging file is never regenerated, even if handoffd consumed it.
  (when (= :prepared (:phase receipt))
    (doseq [out (:outbound receipt)
            :let [staged (fs/path (:staging receipt) (fs/file-name out))]
            :when (fs/exists? staged)]
      (when-not (= (get (:payload-hashes receipt) (str out))
                   (swarm-handoff/sha256 (slurp (str staged))))
        (fail! "AUDIT_PUBLICATION_CORRUPT" "Staged handoff changed; publication stopped."))
      (fs/move staged (fs/path out)))
    (atomic-edn! path (assoc receipt :phase :published)))
  (let [receipt (assoc receipt :phase :published)
        expected (set (get-in receipt [:candidate :inputs]))
        current (swarm-handoff/in-process-task-files)
        current-pairs (set (map #(vector (str %) (swarm-handoff/sha256 (slurp (str %)))) current))]
    ;; Never complete a different/newly accepted task on a replay.
    (when-not (every? expected current-pairs)
      (fail! "AUDIT_COMPLETION_INPUT_CHANGED"
             "Handoff published; current input differs from the audited batch. No new task was completed."))
    (handoff-lib/record-forwarded-inputs! current (:outbound receipt))
    (let [candidate (:candidate receipt)
          pending (swarm-handoff/audit-file (:sender candidate) (:task-id candidate))]
      (when (= candidate (:candidate (swarm-handoff/read-audit pending)))
        (fs/delete-if-exists pending)
        (swarm-handoff/remove-empty-sender-audit-dir! (:sender candidate))))
    ;; Existing completion checks still enforce delivery/approval and clarification gates.
    ;; All files are published before clearing the publication completion guard.
    ;; A crash here is resumed from the same receipt, never from a new draft.
    (atomic-edn! path (assoc receipt :phase :completing))
    (swarm-handoff/complete-current-after-git-handoff! (:headers receipt))
    (let [finished (assoc receipt :phase :complete :completed-at (swarm-handoff/timestamp))]
      (atomic-edn! path finished)
      (when (and (fs/regular-file? (:draft receipt))
                 (= (get-in receipt [:candidate :draft-fingerprint])
                    (swarm-handoff/sha256 (slurp (:draft receipt)))))
        (fs/delete (:draft receipt)))
      (print-publication! finished false))))

(defn complete! [result-file]
  (let [result (read-result! result-file)
        sender (swarm-handoff/sender-role)
        result-hash (swarm-handoff/sha256 (json/generate-string result))
        receipt-path (receipt-file sender (:audit_id result))]
    (when-not (swarm-handoff/role-known? sender) (fail! "AUDIT_ROLE_UNKNOWN" sender))
    (swarm-handoff/with-audit-lock
      (fn []
        (if (fs/regular-file? receipt-path)
          (let [receipt (edn/read-string (slurp (str receipt-path)))]
            (when-not (and (= result (:result receipt))
                           (= (str (fs/real-path (swarm-handoff/git-root))) (:worktree receipt)))
              (fail! "AUDIT_RECEIPT_MISMATCH" "Replay requires the same review and assigned worktree."))
            (if (= :complete (:phase receipt))
              (print-publication! receipt true)
              (finish-publication! receipt-path receipt)))
          (let [ctx (swarm-handoff/prepare-submission! (fs/path (:draft result)) false)
                candidate (:candidate ctx)
                pending (when candidate
                          (:candidate (swarm-handoff/read-audit
                                       (swarm-handoff/audit-file sender (:task-id candidate)))))]
            (when-not (and candidate (= candidate pending)
                           (= (:audit_id result) (swarm-handoff/audit-id candidate))
                           (= (:candidate_commit result) (:commit candidate)))
              (fail! "AUDIT_CANDIDATE_CHANGED"
                     "Request/review the current candidate with swarm_handoff.sh DRAFT; the old result cannot publish it."))
            (fs/create-dirs (fs/path (swarm-handoff/state-dir) "outbox/tmp"))
            (let [staging (fs/create-temp-dir {:dir (fs/path (swarm-handoff/state-dir) "outbox/tmp")
                                              :prefix ".audit-publication-"})
                  outbound (binding [swarm-handoff/*publication-staging* staging]
                             (swarm-handoff/write-handoffs! ctx))
                  receipt {:version 1 :phase :prepared :audit-id (:audit_id result)
                           :candidate candidate :headers (:headers ctx)
                           :result result :result-hash result-hash
                           :worktree (:worktree candidate) :draft (:draft result)
                           :payload-hashes (into {} (map (fn [out]
                                                          [(str out) (swarm-handoff/sha256
                                                                      (slurp (str (fs/path staging (fs/file-name out)))))])
                                                        outbound))
                           :staging (str staging) :outbound (mapv str outbound)}]
              (atomic-edn! receipt-path receipt)
              (finish-publication! receipt-path receipt))))))))

(defn status! [draft]
  (let [ctx (swarm-handoff/prepare-submission! (fs/path draft) false)
        candidate (:candidate ctx)]
    (when-not candidate (fail! "AUDIT_NOT_APPLICABLE" "Only git_handoff requires an audit."))
    (when-not (= candidate (:candidate (swarm-handoff/read-audit
                                      (swarm-handoff/audit-file (:sender ctx) (:task-id candidate)))))
      (fail! "AUDIT_NOT_REQUESTED" "Run swarm_handoff.sh DRAFT to request the current candidate first."))
    (println (json/generate-string
              {:version 1 :audit_id (swarm-handoff/audit-id candidate)
               :candidate_commit (:commit candidate) :draft (str (fs/absolutize draft))
               :decision "review_required" :requirements [] :checks [] :findings []}
              {:pretty true}))))

(defn -main [& args]
  (try
    (case (first args)
      "complete" (if (= 2 (count args)) (complete! (fs/path (second args)))
                     (fail! "AUDIT_USAGE" "swarm_audit.sh complete ./tmp/audit-result.json"))
      "status" (if (= 2 (count args)) (status! (second args))
                   (fail! "AUDIT_USAGE" "swarm_audit.sh status ./tmp/draft"))
      (fail! "AUDIT_USAGE" "swarm_audit.sh status DRAFT | complete RESULT.json"))
    (catch Exception e
      (binding [*out* *err*] (println (ex-message e)))
      (System/exit (or (:exit (ex-data e)) 2)))))

(when (= (str *file*) (System/getProperty "babashka.file"))
  (apply -main *command-line-args*))
