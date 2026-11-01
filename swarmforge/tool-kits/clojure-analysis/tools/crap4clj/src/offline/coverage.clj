(ns offline.coverage
  (:require [clojure.java.io :as io]
            [cloverage.instrument :as instrument]
            [crap4clj.bb-coverage :as bb-coverage]
            [speclj.cloverage :as speclj-coverage]))

(defn source-filename [filename]
  (if-let [url (io/resource filename)]
    (if (= "file" (.getProtocol url))
      (.getCanonicalPath (io/file (.toURI url)))
      filename)
    filename))

(defn -main [& args]
  ;; Cloverage normally binds *file* to a resource-relative filename. Scripts
  ;; that load sibling helpers need the physical file path during evaluation.
  (alter-var-root #'instrument/eval-form
    (fn [original]
      (fn [filename form line-hint instrumented-form]
        (original (source-filename filename) form line-hint instrumented-form))))
  (bb-coverage/enable-bb-sources!)
  (if (= "--speclj" (first args))
    (apply speclj-coverage/-main (rest args))
    (apply bb-coverage/-main args)))
