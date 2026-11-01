(ns dashboard-messages
  (:require [babashka.fs :as fs]
            [cheshire.core :as json]
            [clojure.string :as str]))

;; No retries after a possibly successful paste/Enter. The caller supplies the
;; existing guarded transport and reports phases around its external effects.
(defn receipt-file [root id]
  (when-not (re-matches #"[A-Za-z0-9_-]{1,100}" (or id ""))
    (throw (ex-info "Invalid message id" {:http-status 400})))
  (fs/path root ".swarmforge/dashboard/operations" (str id ".json")))

(defn read-receipt [root id]
  (let [file (receipt-file root id)]
    (when (fs/regular-file? file)
      (json/parse-string (slurp (str file)) true))))

(defn write-receipt! [root receipt]
  (let [file (receipt-file root (:id receipt))
        value (assoc receipt :updated_at (str (java.time.Instant/now)))]
    (fs/create-dirs (fs/parent file))
    (let [tmp (fs/create-temp-file {:dir (fs/parent file) :prefix ".receipt-"})]
      (spit (str tmp) (json/generate-string value))
      (fs/move tmp file {:replace-existing true}))
    value))

(defn receipts [root]
  (let [dir (fs/path root ".swarmforge/dashboard/operations")]
    (if (fs/directory? dir)
      (->> (fs/list-dir dir)
           (filter #(str/ends-with? (str %) ".json"))
           (map #(json/parse-string (slurp (str %)) true))
           (sort-by :created_at) vec)
      [])))

(defn submit! [root {:keys [id role text task retry] :as request} roles deliver!]
  (receipt-file root id)
  (when-not (some #{role} roles)
    (throw (ex-info "Unknown agent role" {:http-status 404})))
  (when-not (and (string? text) (not (str/blank? text)) (<= (count text) 64000)
                 (or (nil? task) (string? task)))
    (throw (ex-info "A message must contain between 1 and 64000 characters" {:http-status 400})))
  ;; Separate operation lock: deliver! itself acquires the existing role lock.
  (agent-lifecycle/with-role-lock! root (str "message-" id)
    (fn []
      (let [previous (read-receipt root id)
            payload (select-keys request [:id :role :text :task :expected_generation])]
        (when (and previous (not= payload (select-keys previous [:id :role :text :task :expected_generation])))
          (throw (ex-info "Message id already belongs to a different payload" {:http-status 409})))
        (if (and previous (not (and (true? retry) (= "not_sent" (:status previous)))))
          previous
          (let [state (atom (write-receipt! root
                             (merge payload
                                    {:created_at (or (:created_at previous) (str (java.time.Instant/now)))
                                     :status "attempting" :attempts (inc (or (:attempts previous) 0))})))
                phase! (fn [phase]
                         (reset! state (write-receipt! root
                                        (if (map? phase)
                                          (merge @state phase)
                                          (assoc @state :status (name phase) :last_phase (name phase))))))]
            (try
              (deliver! (fn [phase] (phase! phase)))
              (phase! :submitted)
              @state
              (catch Exception e
                (reset! state (write-receipt! root
                               (assoc @state
                                      :status (if (= "attempting" (:status @state)) "not_sent" "uncertain")
                                      :error (.getMessage e))))
                @state))))))))

(defn complete! [root id submit-enter!]
  (receipt-file root id)
  (agent-lifecycle/with-role-lock! root (str "message-" id)
    (fn []
      (let [receipt (or (read-receipt root id)
                        (throw (ex-info "Unknown message operation" {:http-status 404})))]
        (if (= "submitted" (:status receipt))
          receipt
          (do
            (when-not (and (= "inserted" (:last_phase receipt))
                            (:session receipt) (:generation receipt))
              (throw (ex-info "Cannot prove that Enter was not already attempted; completion disabled."
                              {:http-status 409})))
            (let [state (atom receipt)
                  submitting! #(reset! state (write-receipt! root
                                               (assoc @state :status "submitting" :last_phase "submitting")))]
              (try
                (submit-enter! receipt submitting!)
                (write-receipt! root (dissoc (assoc @state :status "submitted" :last_phase "submitted") :error))
                (catch Exception e
                  (write-receipt! root (assoc @state :status "uncertain" :error (.getMessage e))))))))))))
