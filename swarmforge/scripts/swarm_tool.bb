#!/usr/bin/env bb

(ns swarm-tool
  (:require [babashka.fs :as fs]
            [clojure.java.shell :as sh]
            [clojure.string :as str]))

(load-file (str (fs/path (fs/parent *file*) "swarm_startup.bb")))

(def catalog
  {"coverage-js" {:builtin :node-coverage}
   "mutation-js" {:local-only true}
   "crap-js" {:local-only true}
   "hammer" {:local-only true}
   "gherkin-parser" {:source "github.com/unclebob/Acceptance-Pipeline-Specification"
                     :bb-task "gherkin-parser"}
   "ir-dry-checker" {:source "github.com/unclebob/Acceptance-Pipeline-Specification"
                     :bb-task "gherkin-ir-dry-checker"}
   "gherkin-mutator" {:source "github.com/unclebob/Acceptance-Pipeline-Specification"
                      :bb-task "gherkin-mutator"}
   "crap4clj" {:source "github.com/unclebob/crap4clj" :bb-task "crap"
               :needs ["cloverage"]}
   "dry4clj" {:source "github.com/unclebob/dry4clj" :bb-task "dry4clj"}
   "clj-mutate" {:source "github.com/unclebob/clj-mutate" :bb-task "mutate"
                 :needs ["cloverage"]}
   "cloverage" {:mvn "cloverage/cloverage" :main "cloverage.coverage"
                :paths ["src" "spec" "test"]
                :extra-deps {"speclj/speclj" "3.13.0"}
                :args ["-p" "src" "-s" "spec" "-s" "test" "-r" "speclj"]}
   "speclj" {:mvn "speclj/speclj" :main "speclj.main" :version "3.13.0"
             :paths ["src" "spec" "test"]
             :args ["-c" "spec"]}
   "speclj-structure-check" {:source "github.com/unclebob/speclj-structure-check"
                             :bb-task "check"}
   "crap4go" {:source "github.com/unclebob/crap4go" :bb-task "crap4go"}
   "dry4go" {:source "github.com/unclebob/dry4go" :bb-task "dry4go"}
   "mutate4go" {:source "github.com/unclebob/mutate4go" :bb-task "mutate4go"}
   "crap4java" {:source "github.com/unclebob/crap4java" :bb-task "crap4java"}
   "dry4java" {:source "github.com/unclebob/dry4java" :bb-task "dry4java"}
   "mutate4java" {:source "github.com/unclebob/mutate4java" :bb-task "mutate4java"}})

(def usage-text
  (str "Usage:\n"
       "  swarm_tool.sh require <tool>\n"
       "  swarm_tool.sh ensure <tool>\n\n"
       "  swarm_tool.sh --offline ensure <tool>\n"
       "  swarm_tool.sh install-local <tool> <directory-with-run>\n"
       "  swarm_tool.sh export <new-directory>\n"
       "  swarm_tool.sh import <bundle-directory>\n"
       "  swarm_tool.sh offline on|off\n\n"
       "Tools: " (str/join ", " (sort (keys catalog)))))

(defn usage []
  (binding [*out* *err*]
    (println usage-text)))

(defn exit! [status message]
  (binding [*out* *err*]
    (when message
      (println message)))
  (System/exit status))

(defn sq [value]
  (str "'" (str/replace (str value) #"'" "'\"'\"'") "'"))

(defn roles-at? [root]
  (and root (fs/exists? (fs/path root ".swarmforge" "roles.tsv"))))

(defn project-at? [root]
  (and root (or (roles-at? root)
                (fs/exists? (fs/path root "swarmforge/swarmforge.conf")))))

(defn git-common-dir []
  (let [git (sh/sh "git" "rev-parse" "--git-common-dir")]
    (when (zero? (:exit git))
      (let [path (fs/path (str/trim (:out git)))]
        (str (if (fs/absolute? path) path (fs/absolutize path)))))))

(defn project-root []
  (or (let [parent (some-> (git-common-dir) fs/parent str)]
        (when (project-at? parent) parent))
      (let [git (sh/sh "git" "rev-parse" "--show-toplevel")
            root (when (zero? (:exit git)) (str/trim (:out git)))]
        (when (project-at? root) root))
      (when (project-at? (fs/cwd)) (fs/cwd))
      (exit! 1 "Cannot find SwarmForge project root")))

(defn canonical-tool [tool]
  (str/lower-case (or tool "")))

(defn tool-spec [tool]
  (when (and (contains? swarm-startup/aps-tools (canonical-tool tool))
             (not (:aps-enabled (swarm-startup/policy (project-root)))))
    (exit! 2 (str "TOOL_DISABLED: " tool " is disabled by swarmforge/startup.edn."
                  " No analysis was run or passed.")))
  (or (get catalog (canonical-tool tool))
      (exit! 1 (str "Unknown tool: " tool "\n\n" usage-text))))

(defn bin-dir [root]
  (fs/path root ".swarmforge" "bin"))

(defn wrapper-path [root tool]
  (fs/path (bin-dir root) (canonical-tool tool)))

(defn source-dir [root source]
  (if-let [override (not-empty (System/getenv "SWARMFORGE_TOOL_SRC"))]
    (fs/path override)
    (fs/path root ".swarmforge" "tools" (last (str/split source #"/")))))

(def ^:dynamic *offline* false)

(defn offline? [root]
  (or *offline*
      (= "1" (System/getenv "SWARMFORGE_OFFLINE"))
      (fs/exists? (fs/path root ".swarmforge/offline"))))

(defn local-tool-dir [root tool]
  (fs/path root ".swarmforge/local-tools" (canonical-tool tool)))

(defn local-tool? [root tool]
  (fs/executable? (fs/path (local-tool-dir root tool) "run")))

(def bundled-kit-dir
  (fs/normalize (fs/path (fs/parent (fs/absolutize *file*)) ".."
                         "tool-kits/clojure-analysis")))

(defn bundled-tool [root tool spec]
  (let [dir (first (filter #(fs/executable? (fs/path % "run"))
                           [(fs/path root "swarmforge/tool-kits/clojure-analysis/tools" tool)
                            (fs/path bundled-kit-dir "tools" tool)]))]
    (when (and dir
               (or (offline? root)
                   (not (and (:source spec) (System/getenv "SWARMFORGE_TOOL_SRC")))))
      dir)))

(defn needed-tools [tool]
  (vec (or (:needs (tool-spec tool)) [])))

(defn missing-tool [root tool]
  (first (remove #(and (fs/executable? (wrapper-path root %))
                      (or (not (offline? root)) (local-tool? root %)))
                 (if (local-tool? root tool)
                   [tool]
                   (cons tool (needed-tools tool))))))

(defn require-tool! [tool]
  (tool-spec tool)
  (let [root (project-root)
        missing (missing-tool root tool)]
    (if missing
      (exit! 1 (str "MISSING: " missing "\nRun: swarm_tool.sh ensure " missing))
      (do (println "OK:" tool (str (wrapper-path root tool)))
          (System/exit 0)))))

(defn clone-source! [dir source]
  (fs/create-dirs (fs/parent dir))
  (let [url (str "https://" source ".git")
        result (sh/sh "git" "clone" "--depth" "1" url (str dir))]
    (when-not (zero? (:exit result))
      (exit! 1 (str "Failed to clone " url "\n" (:err result) (:out result))))))

(defn ensure-source! [root source]
  (let [dir (source-dir root source)]
    (when-not (fs/exists? (fs/path dir "bb.edn"))
      (when (System/getenv "SWARMFORGE_TOOL_SRC")
        (exit! 1 (str "SWARMFORGE_TOOL_SRC is missing bb.edn: " dir)))
      (clone-source! dir source))
    dir))

(defn mutate-rewrite-bash []
  (str "args=()\n"
       "scan=\n"
       "while [ $# -gt 0 ]; do\n"
       "  case \"$1\" in\n"
       "    --mutate-all) shift ;;\n"
       "    --scan|--update-manifest) scan=1; args+=(\"$1\"); shift ;;\n"
       "    --max-workers) shift; [ $# -gt 0 ] && shift ;;\n"
       "    *) args+=(\"$1\"); shift ;;\n"
       "  esac\n"
       "done\n"
       "if [ -z \"$scan\" ]; then args+=(--max-workers 4); fi\n"
       "set -- \"${args[@]}\"\n"))

(defn gherkin-rewrite-bash []
  (str "args=()\n"
       "while [ $# -gt 0 ]; do\n"
       "  case \"$1\" in\n"
       "    --level)\n"
       "      if [ \"${2:-}\" = full ]; then args+=(--level hard); else args+=(\"$1\" \"$2\"); fi\n"
       "      shift; [ $# -gt 0 ] && shift ;;\n"
       "    --workers) shift; [ $# -gt 0 ] && shift ;;\n"
       "    *) args+=(\"$1\"); shift ;;\n"
       "  esac\n"
       "done\n"
       "args+=(--workers 4)\n"
       "set -- \"${args[@]}\"\n"))

(defn rewrite-bash [tool]
  (cond
    (#{"clj-mutate" "mutate4go" "mutate4java"} tool) (mutate-rewrite-bash)
    (= "gherkin-mutator" tool) (gherkin-rewrite-bash)
    :else ""))

(defn write-wrapper! [path body]
  (fs/create-dirs (fs/parent path))
  (spit (str path) (str "#!/usr/bin/env bash\n" body))
  (fs/set-posix-file-permissions path "rwxr-xr-x")
  path)

(defn write-local-wrapper! [root tool]
  (write-wrapper!
   (wrapper-path root tool)
   (str "set -euo pipefail\n"
        "tool_dir=\"$(cd -- \"$(dirname -- \"$0\")/../local-tools/" tool "\" && pwd)\"\n"
        "export SWARMFORGE_OFFLINE=1\n"
        "export npm_config_offline=true\n"
        (rewrite-bash tool)
        "exec \"$tool_dir/run\" \"$@\"\n")))

(defn validate-local! [dir]
  (when (fs/sym-link? dir)
    (exit! 1 (str "LOCAL_TOOL_INVALID: symbolic link directory: " dir)))
  (when-not (and (fs/directory? dir) (fs/executable? (fs/path dir "run")))
    (exit! 1 (str "LOCAL_TOOL_INVALID: executable run missing in " dir)))
  ;; A portable bundle cannot depend on links to files outside its directory.
  (doseq [p (fs/glob dir "**" {:hidden true})]
    (when (fs/sym-link? p)
      (exit! 1 (str "LOCAL_TOOL_INVALID: materialize symbolic link before packaging: " p)))))

(defn install-local! [tool directory]
  (tool-spec tool)
  (let [root (project-root)
        tool (canonical-tool tool)
        src (fs/absolutize directory)
        dest (local-tool-dir root tool)]
    (validate-local! src)
    (when (fs/exists? dest)
      (exit! 1 (str "LOCAL_TOOL_EXISTS: " dest)))
    (fs/create-dirs (fs/parent dest))
    (fs/copy-tree src dest)
    (write-local-wrapper! root tool)
    (println "INSTALLED_LOCAL:" tool (str dest))))

(defn bundle-tools [directory]
  (let [tools-dir (fs/path directory "tools")
        marker (fs/path directory "swarmforge-tools-v1")]
    (when-not (and (fs/exists? (fs/path directory "swarmforge-tools-v1"))
                   (= "1" (str/trim (slurp (str marker))))
                   (fs/directory? tools-dir)
                   (not (fs/sym-link? tools-dir)))
      (exit! 1 (str "INVALID_BUNDLE: " directory)))
    (let [dirs (sort (fs/list-dir tools-dir))]
      (when (empty? dirs) (exit! 1 "EMPTY_BUNDLE"))
      (doseq [dir dirs]
        (tool-spec (str (fs/file-name dir)))
        (validate-local! dir))
      dirs)))

(defn export-bundle! [directory]
  (let [root (project-root)
        src (fs/path root ".swarmforge/local-tools")
        dest (fs/absolutize directory)]
    (when (fs/exists? dest) (exit! 1 (str "BUNDLE_EXISTS: " dest)))
    (when-not (and (fs/directory? src) (seq (fs/list-dir src)))
      (exit! 1 "EMPTY_BUNDLE: install local tools first"))
    (doseq [dir (fs/list-dir src)] (validate-local! dir))
    (when (.startsWith dest (fs/absolutize src))
      (exit! 1 "INVALID_DESTINATION: export outside local-tools"))
    (fs/create-dirs dest)
    (fs/copy-tree src (fs/path dest "tools"))
    (spit (str (fs/path dest "swarmforge-tools-v1")) "1\n")
    (println "EXPORTED:" (str dest))))

(defn import-bundle! [directory]
  (let [dirs (bundle-tools directory)
        root (project-root)]
    ;; Validate every destination before installing anything.
    (doseq [dir dirs]
      (when (fs/exists? (local-tool-dir root (str (fs/file-name dir))))
        (exit! 1 (str "LOCAL_TOOL_EXISTS: " (fs/file-name dir)))))
    (doseq [dir dirs] (install-local! (str (fs/file-name dir)) dir))
    (spit (str (fs/path root ".swarmforge/offline")) "1\n")
    (println "OFFLINE: on")))

(defn write-bb-wrapper! [root tool bb-task src-dir]
  (let [target (wrapper-path root tool)
        config (str (fs/path src-dir "bb.edn"))]
    (write-wrapper!
     target
     (str (rewrite-bash tool)
          "exec bb --config " (sq config) " " bb-task " \"$@\"\n"))))

(defn edn-paths [paths]
  (str/join " " (map pr-str (or paths []))))

(defn coord-dep [coord version]
  (str coord " {:mvn/version " (pr-str version) "}"))

(defn edn-deps [spec]
  (let [main (coord-dep (:mvn spec) (or (:version spec) "RELEASE"))
        extra (map (fn [[coord version]] (coord-dep coord version))
                   (or (:extra-deps spec) {}))]
    (str/join " " (cons main extra))))

(defn write-mvn-wrapper! [root tool spec]
  (let [target (wrapper-path root tool)
        deps (str "{:paths [" (edn-paths (:paths spec)) "] :deps {" (edn-deps spec) "}}")
        args (str/join " " (or (:args spec) []))]
    (write-wrapper!
     target
     (str (rewrite-bash tool)
          "exec clojure -Sdeps " (sq deps) " -M -m " (:main spec)
          (when (seq args) (str " " args))
          " \"$@\"\n"))))

(defn install-one! [tool]
  (let [spec (tool-spec tool)
        root (project-root)
        name (canonical-tool tool)
        target (if-let [bb-task (:bb-task spec)]
                 (write-bb-wrapper! root name bb-task
                                    (ensure-source! root (:source spec)))
                 (write-mvn-wrapper! root name spec))]
    (println "INSTALLED:" name (str target))))

(defn ensure-tool! [tool]
  (let [spec (tool-spec tool)
        root (project-root)
        tool (canonical-tool tool)]
    (cond
      (local-tool? root tool) (write-local-wrapper! root tool)
      (bundled-tool root tool spec)
      (do (doseq [dep (needed-tools tool)] (ensure-tool! dep))
          (install-local! tool (bundled-tool root tool spec)))
      (:builtin spec)
      (do (write-wrapper! (fs/path (local-tool-dir root tool) "run")
                          "exec node --test --experimental-test-coverage \"$@\"\n")
          (write-local-wrapper! root tool))
      (or (offline? root) (:local-only spec))
      (exit! 1 (str "LOCAL_TOOL_REQUIRED: " tool
                    "\nUse swarm_tool.sh install-local " tool " <directory-with-run> or import <bundle>."))
      :else
      (do (doseq [dep (needed-tools tool)] (ensure-tool! dep))
          (install-one! tool)))))

(defn -main [& args]
  (when (some #{"--help" "-h"} args)
    (usage)
    (System/exit 0))
  (when (= "--offline" (first args))
    (binding [*offline* true] (apply -main (rest args)))
    (System/exit 0))
  (when-not (or (= 2 (count args))
                (and (= "install-local" (first args)) (= 3 (count args))))
    (usage)
    (System/exit 1))
  (let [[command tool] args]
    (case command
      "require" (require-tool! tool)
      "ensure" (ensure-tool! tool)
      "install-local" (install-local! tool (nth args 2))
      "export" (export-bundle! tool)
      "import" (import-bundle! tool)
      "offline" (let [path (fs/path (project-root) ".swarmforge/offline")]
                  (case tool
                    "on" (spit (str path) "1\n")
                    "off" (fs/delete-if-exists path)
                    (exit! 1 "Usage: swarm_tool.sh offline on|off")))
      (do (usage)
          (System/exit 1)))))

(when (= (str *file*) (System/getProperty "babashka.file"))
  (apply -main *command-line-args*))
