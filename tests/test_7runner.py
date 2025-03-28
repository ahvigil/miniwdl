import unittest
import logging
import tempfile
import random
import os
import shutil
import time
import docker
import platform
from testfixtures import log_capture
from .context import WDL
from unittest.mock import patch

class RunnerTestCase(unittest.TestCase):
    """
    Base class for new runner test cases
    """

    @classmethod
    def setUpClass(cls):
        logging.basicConfig(level=logging.DEBUG, format='%(name)s %(levelname)s %(message)s')
        logger = logging.getLogger(cls.__name__)
        cfg = WDL.runtime.config.Loader(logger, [])

    def setUp(self):
        """
        initialize docker & provision temporary directory for a test (self._dir)
        """
        self._dir = tempfile.mkdtemp(prefix=f"miniwdl_test_{self.id()}_")

    def tearDown(self):
        if not getattr(self, "_keep_dir", False):
            shutil.rmtree(self._dir)

    def _run(self, wdl:str, inputs = None, task = None, expected_exception: Exception = None, cfg = None):
        """
        run workflow/task & return outputs dict
        """
        logger = logging.getLogger(self.id())
        cfg = cfg or WDL.runtime.config.Loader(logger, [])
        try:
            with tempfile.NamedTemporaryFile(dir=self._dir, suffix=".wdl", delete=False) as outfile:
                outfile.write(wdl.encode("utf-8"))
                wdlfn = outfile.name
            doc = WDL.load(wdlfn)
            target = doc.workflow or doc.tasks[0]
            if task:
                target = next((t for t in doc.tasks if t.name == task), None)
            assert target
            if isinstance(inputs, dict):
                inputs = WDL.values_from_json(inputs, target.available_inputs, target.required_inputs)
            rundir, outputs = WDL.runtime.run(cfg, target, (inputs or WDL.Env.Bindings()), run_dir=self._dir)
            self._rundir = rundir
        except Exception as exn:
            while isinstance(exn, WDL.runtime.RunFailed):
                exn = exn.__context__
            if expected_exception:
                self.assertIsInstance(exn, expected_exception)
                return exn
            raise
        self.assertIsNone(expected_exception, str(expected_exception) + " not raised")
        return WDL.values_to_json(outputs)

class TestDirectoryIO(RunnerTestCase):
    def test_coercion(self):
        assert WDL.Type.Directory().coerces(WDL.Type.String())
        d = WDL.Value.String("foo").coerce(WDL.Type.Directory())
        assert isinstance(d, WDL.Value.Directory)
        assert d.value == "foo"

    def test_basic_directory(self):
        wdl = R"""
        version development
        workflow w {
            input {
                Directory d
            }
            call t {
                input:
                    d = d
            }
            output {
                Int dsz = round(size(t.files))
            }
        }
        task t {
            input {
                Directory d
                Boolean touch = false
            }
            command {
                set -euxo pipefail
                mkdir outdir
                cp "~{d}"/* outdir/
                if [ "~{touch}" == "true" ]; then
                    touch "~{d}"/foo
                fi
                >&2 ls -Rl
            }
            output {
                Array[File] files = glob("outdir/*.txt")
            }
        }
        """
        os.makedirs(os.path.join(self._dir, "d"))
        with open(os.path.join(self._dir, "d/alice.txt"), mode="w") as outfile:
            print("Alice", file=outfile)
        with open(os.path.join(self._dir, "d/bob.txt"), mode="w") as outfile:
            print("Bob", file=outfile)
        outp = self._run(wdl, {"d": os.path.join(self._dir, "d")})
        assert outp["dsz"] == 10

        with self.assertRaises(WDL.runtime.error.RunFailed):
            self._run(wdl, {"d": os.path.join(self._dir, "d"), "t.touch": True})

        cfg = WDL.runtime.config.Loader(logging.getLogger(self.id()), [])
        cfg.override({"file_io": {"copy_input_files": True}})
        outp = self._run(wdl, {"d": os.path.join(self._dir, "d"), "t.touch": True}, cfg=cfg)
        assert outp["dsz"] == 10

    def test_directory_output(self):
        wdl = R"""
        version development
        workflow w {
            input {
                Directory d
            }
            call t {
                input:
                    d = d
            }
            output {
                Array[Directory] d_out = t.d_out
            }
        }
        task t {
            input {
                Directory d
            }
            command {
                set -euxo pipefail
                mkdir -p outdir/foo
                cd outdir
                echo foobar > foo/bar
                ln -s foo/bar baz
                >&2 ls -Rl
            }
            output {
                Array[Directory] d_out = ["~{d}", "outdir"]
            }
        }
        """

        os.makedirs(os.path.join(self._dir, "d"))
        with open(os.path.join(self._dir, "d/alice.txt"), mode="w") as outfile:
            print("Alice", file=outfile)
        with open(os.path.join(self._dir, "d/bob.txt"), mode="w") as outfile:
            print("Bob", file=outfile)
        outp = self._run(wdl, {"d": os.path.join(self._dir, "d")})

        assert len(outp["d_out"]) == 2
        assert os.path.islink(outp["d_out"][0])
        assert os.path.realpath(outp["d_out"][0]) == os.path.realpath(os.path.join(self._dir, "d"))
        assert os.path.isdir(outp["d_out"][1])
        assert os.path.islink(outp["d_out"][1])
        assert os.path.basename(outp["d_out"][1]) == "outdir"
        assert os.path.isfile(os.path.join(outp["d_out"][1], "foo/bar"))
        assert os.path.islink(os.path.join(outp["d_out"][1], "baz"))
        assert os.path.isfile(os.path.join(outp["d_out"][1], "baz"))
        assert os.path.isfile(os.path.join(os.path.dirname(outp["d_out"][1]), ".WDL_Directory"))

        cfg = WDL.runtime.config.Loader(logging.getLogger(self.id()), [])
        cfg.override({"file_io": {"output_hardlinks": True}})
        outp = self._run(wdl, {"d": os.path.join(self._dir, "d")}, cfg=cfg)
        assert len(outp["d_out"]) == 2
        assert not os.path.islink(outp["d_out"][0])
        assert os.path.realpath(outp["d_out"][0]) != os.path.realpath(os.path.join(self._dir, "d"))
        assert os.path.isdir(outp["d_out"][1])
        assert not os.path.islink(outp["d_out"][1])
        assert os.path.basename(outp["d_out"][1]) == "outdir"
        assert os.path.isfile(os.path.join(outp["d_out"][1], "foo/bar"))
        assert os.path.islink(os.path.join(outp["d_out"][1], "baz"))
        assert os.path.isfile(os.path.join(outp["d_out"][1], "baz"))
        assert os.path.isfile(os.path.join(outp["d_out"][1], "..", ".WDL_Directory"))

        outp = self._run(R"""
            version development
            task t {
                command {}
                output {
                    Directory? d_out = "bogus/dirname"
                }
            }
            """, {})
        assert outp["d_out"] is None

    def test_output_input(self):
        # test outputting files/subdirectories inside input Directory
        wdl = R"""
        version development
        task t {
            input {
                Directory d
            }
            command {}
            output {
                Array[File] files = ["~{d}/alice.txt", "~{d}/sub/bob.txt"]
                Array[Directory] dirs = ["~{d}/sub/dir"]
            }
        }
        """
        os.makedirs(os.path.join(self._dir, "d/sub/dir"))
        with open(os.path.join(self._dir, "d/alice.txt"), mode="w") as outfile:
            print("Alice", file=outfile)
        with open(os.path.join(self._dir, "d/sub/bob.txt"), mode="w") as outfile:
            print("Bob", file=outfile)
        with open(os.path.join(self._dir, "d/sub/dir/carol.txt"), mode="w") as outfile:
            print("Carol", file=outfile)
        outp = self._run(wdl, {"d": os.path.join(self._dir, "d")})
        assert len(outp["files"]) == 2
        for fn in outp["files"]:
            assert os.path.isfile(fn)
        assert len(outp["dirs"]) == 1
        assert os.path.isdir(outp["dirs"][0])

    def test_errors(self):
        self._run(R"""
            version development
            task t {
                command <<<
                    mkdir outdir
                    ln -s /etc/passwd outdir/owned
                >>>
                output {
                    Directory d_out = "outdir"
                }
            }
            """, {}, expected_exception=WDL.runtime.error.OutputError)

        self._run(R"""
            version development
            task t {
                command <<<
                    touch secret
                    mkdir outdir
                    ln -s ../secret outdir/owned
                    >&2 ls -Rl
                >>>
                output {
                    Directory d_out = "outdir/"
                }
            }
            """, {}, expected_exception=WDL.runtime.error.OutputError)

        self._run(R"""
            version development
            task t {
                command <<<
                    mkdir outdir
                    touch outdir/secret
                    ln -s outdir/secret outdir/owned
                    rm outdir/secret
                    >&2 ls -Rl
                >>>
                output {
                    Directory d_out = "outdir"
                }
            }
            """, {}, expected_exception=WDL.runtime.error.OutputError)

        self._run(R"""
            version development
            task t {
                command <<<
                    touch outdir
                >>>
                output {
                    Directory d_out = "outdir"
                }
            }
            """, {}, expected_exception=WDL.runtime.error.OutputError)

        self._run(R"""
            version development
            task t {
                command <<<
                    mkdir outdir
                >>>
                output {
                    File f_out = "outdir"
                }
            }
            """, {}, expected_exception=WDL.runtime.error.OutputError)

        with open(os.path.join(self._dir, "foo.txt"), mode="w") as outfile:
            print("foo", file=outfile)
        self._run(R"""
            version development
            task t {
                input {
                    File f
                }
                command <<<
                    echo `dirname "~{f}"` > outdir
                >>>
                output {
                    Directory d_out = read_string("outdir")
                }
            }
            """, {"f": os.path.join(self._dir, "foo.txt")},
                  expected_exception=WDL.runtime.error.OutputError)

        self._run(R"""
            version development
            task t {
                input {
                    File f
                }
                command <<<
                    echo $(pwd) > outdir
                >>>
                output {
                    Directory d_out = read_string("outdir")
                }
            }
            """, {"f": os.path.join(self._dir, "foo.txt")},
                  expected_exception=WDL.runtime.error.OutputError)

class TestNoneLiteral(RunnerTestCase):
    def test_none_eval(self):
        wdl = R"""
        version 1.1
        struct Car {
            String make
            String? model
        }
        workflow wf {
            input {
                Int? x = None
                Array[Car?] ac = [None]
                Array[Int?] a = [x, None]
            }
            if (x == None) {
                Boolean flag1 = true
            }
            if (defined(None)) {
                Boolean flag2 = true
            }
            output {
                Boolean b1 = defined(flag1)
                Boolean b2 = defined(flag2)
                Car c = Car {
                    make: "One",
                    model: None
                }
                Array[Int?] a2 = select_all([x, None])
            }
        }
        """
        outp = self._run(wdl, {})
        assert outp["b1"]
        assert not outp["b2"]
        assert outp["c"]["model"] is None
        assert outp["a2"] == []

        outp = self._run(wdl, {"x": 42})
        assert not outp["b1"]
        assert not outp["b2"]
        assert outp["c"]["model"] is None
        assert outp["a2"] == [42]

class TestCallAfter(RunnerTestCase):
    def test_call_after(self):
        wdl = R"""
        version 1.1
        task nop {
            input {
                Int? y = 0
            }
            command {}
            output {
                Int x = 1
            }
        }
        workflow w {
            call nop as A
            scatter (i in range(2)) {
                call nop as B
            }
            if (false) {
                call nop as C {
                    input:
                    y = 3
                }
            }
            call nop as D after A after B after C
            scatter (i in range(2)) {
                call nop after D {
                    input:
                        y = A.x
                }
            }
        }
        """
        outp = self._run(wdl, {})
        assert outp["nop.x"] == [1, 1]

        with self.assertRaises(WDL.Error.NoSuchCall):
            self._run(R"""
            version 1.1
            task nop {
                input {}
                command {}
                output {
                    Int x = 1
                }
            }
            workflow w {
                call nop as A
                call nop after B
            }
            """)

        with self.assertRaises(WDL.Error.CircularDependencies):
            self._run(R"""
            version 1.1
            task nop {
                input {}
                command {}
                output {
                    Int x = 1
                }
            }
            workflow w {
                call nop as A
                call nop after A after nop
            }
            """)

class TestDownload(RunnerTestCase):
    count_wdl: str = R"""
        version 1.0
        workflow count {
            input {
                Array[File] files
            }
            scatter (file in files) {
                Array[String] file_lines = read_lines(file)
            }
            output {
                Int lines = length(flatten(file_lines))
            }
        }
        """

    def test_download_input_files(self):
        self._run(self.count_wdl, {"files": [
            "gs://gcp-public-data-landsat/LC08/01/044/034/LC08_L1GT_044034_20130330_20170310_01_T2/LC08_L1GT_044034_20130330_20170310_01_T2_MTL.txt",
            "https://raw.githubusercontent.com/chanzuckerberg/miniwdl/main/tests/alyssa_ben.txt"
        ]})
        self._run(self.count_wdl, {"files": [
            "gs://gcp-public-data-landsat/LC08/01/044/034/LC08_L1GT_044034_20130330_20170310_01_T2/LC08_L1GT_044034_20130330_20170310_01_T2_MTL.txt",
            "https://raw.githubusercontent.com/chanzuckerberg/miniwdl/main/nonexistent12345.txt", "https://raw.githubusercontent.com/chanzuckerberg/miniwdl/main/tests/alyssa_ben.txt"
        ]}, expected_exception=WDL.runtime.DownloadFailed)
        self._run(self.count_wdl, {"files": ["gs://8675309"]}, expected_exception=WDL.runtime.DownloadFailed)

    @log_capture()
    def test_download_cache1(self, capture):
        cfg = WDL.runtime.config.Loader(logging.getLogger(self.id()))
        cfg.override({
            "download_cache": {
                "put": True,
                "get": True,
                "dir": os.path.join(self._dir, "cache"),
                "disable_patterns": ["https://google.com/*"]
            }
        })
        inp = {"files": ["https://google.com/robots.txt", "https://raw.githubusercontent.com/chanzuckerberg/miniwdl/main/tests/alyssa_ben.txt"]}
        self._run(self.count_wdl, inp, cfg=cfg)
        self._run(self.count_wdl, inp, cfg=cfg)
        logs = [str(record.msg) for record in capture.records if str(record.msg).startswith("processed input URIs")]
        self.assertTrue("downloaded: 2" in logs[0])
        # alyssa_ben.txt is cached on second run through (robots.txt not due to disable_patterns)
        self.assertTrue("downloaded: 1" in logs[1])
        self.assertTrue("cached: 1" in logs[1])

    @log_capture()
    def test_download_cache2(self, capture):
        cfg = WDL.runtime.config.Loader(logging.getLogger(self.id()))
        cfg.override({
            "download_cache": {
                "put": True,
                "get": True,
                "dir": os.path.join(self._dir, "cache2"),
                "enable_patterns": ["https://raw.githubusercontent.com/chanzuckerberg/*"]
            }
        })
        inp = {"files": ["https://google.com/robots.txt", "https://raw.githubusercontent.com/chanzuckerberg/miniwdl/main/tests/alyssa_ben.txt"]}
        self._run(self.count_wdl, inp, cfg=cfg)
        self._run(self.count_wdl, inp, cfg=cfg)
        logs = [str(record.msg) for record in capture.records if str(record.msg).startswith("processed input URIs")]
        self.assertTrue("downloaded: 2" in logs[0])
        # alyssa_ben.txt is cached on second run through
        self.assertTrue("downloaded: 1" in logs[1])
        self.assertTrue("cached: 1" in logs[1])

    @log_capture()
    def test_download_cache3(self, capture):
        cfg = WDL.runtime.config.Loader(logging.getLogger(self.id()))
        cfg.override({
            "download_cache": {
                "put": True,
                "get": True,
                "dir": os.path.join(self._dir, "cache"),
            }
        })
        inp = {"files": ["s3://1000genomes/CHANGELOG", "https://raw.githubusercontent.com/chanzuckerberg/miniwdl/main/tests/alyssa_ben.txt?xxx"]}
        self._run(self.count_wdl, inp, cfg=cfg)
        self._run(self.count_wdl, inp, cfg=cfg)
        logs = [str(record.msg) for record in capture.records if str(record.msg).startswith("processed input URIs")]
        # cache isn't used for alyssa_ben.txt due to presence of query string
        self.assertTrue("downloaded: 2" in logs[0])
        self.assertTrue("downloaded: 1" in logs[1])
        assert next(record for record in capture.records if "AWS credentials" in str(record.msg))

    @log_capture()
    def test_download_cache4(self, capture):
        cfg = WDL.runtime.config.Loader(logging.getLogger(self.id()))
        cfg.override({
            "download_cache": {
                "put": True,
                "get": True,
                "dir": os.path.join(self._dir, "cache4"),
                "ignore_query": True
            },
            # test JSON logging:
            "logging": { "json": True }
        })
        inp = {"files": ["https://raw.githubusercontent.com/chanzuckerberg/miniwdl/main/tests/alyssa_ben.txt?xxx"]}
        self._run(self.count_wdl, inp, cfg=cfg)
        logs = [str(record.msg) for record in capture.records if "processed input URIs" in str(record.msg)]
        n_logs = len(logs)
        assert "'downloaded': 1" in logs[0]
        self._run(self.count_wdl, inp, cfg=cfg)
        # cache used with ignore_query
        logs = [str(record.msg) for record in capture.records if "processed input URIs" in str(record.msg)][n_logs:]
        assert "'downloaded': 0" in logs[0], logs[0]
        assert "'cached': 1" in logs[0]

    @log_capture()
    def test_download_cache5(self, capture):
        # passing workflow-level URI inputs through to task, which should find them in the cache
        wdl5 = """
        version 1.0
        task t {
            input {
                File f1
                File f2
            }
            command {}
            output {
                Int size2 = floor(size(f1) + size(f2))
            }
        }
        task u {
            input {
                File f1
                File f2 = "https://raw.githubusercontent.com/chanzuckerberg/miniwdl/main/tests/alyssa_ben.txt"
            }
            command {}
            output {
                Int size2 = floor(size(f1) + size(f2))
            }
        }
        workflow w {
            input {
                Array[File] af1
            }
            scatter (f1 in af1) {
                call t { input: f1 = f1 }
            }
            call u
            output {
                Array[Int] sizes = t.size2
                Int size2 = u.size2
            }
        }
        """
        cfg = WDL.runtime.config.Loader(logging.getLogger(self.id()))
        cfg.override({
            "download_cache": {
                "put": True,
                "get": True,
                "dir": os.path.join(self._dir, "cache5"),
                "disable_patterns": ["*://google.com/*"]
            },
            "logging": { "json": True }
        })
        inp = {
            "af1": ["s3://1000genomes/CHANGELOG", "gs://gcp-public-data-landsat/LC08/01/044/034/LC08_L1GT_044034_20130330_20170310_01_T2/LC08_L1GT_044034_20130330_20170310_01_T2_MTL.txt"],
            "t.f2": "https://google.com/robots.txt",
            "u.f1": "https://google.com/robots.txt"
        }
        self._run(wdl5, inp, cfg=cfg)
        for record in capture.records:
            msg = str(record.msg)
            if (
                "t:call-t" not in record.name
                and "t:call-u" not in record.name
                and "processed input URIs" in msg
            ):
                self.assertTrue("'downloaded': 4" in msg)
            if "t:call-t" in record.name and "processed input URIs" in msg:
                self.assertTrue("'downloaded': 0" in msg)
                self.assertTrue("'cached': 2" in msg)
            if "t:call-u" in record.name and "processed input URIs" in msg:
                self.assertTrue("'downloaded': 0" in msg)
                self.assertTrue("'cached': 2" in msg)

    @log_capture()
    def test_aws_directory(self, capture):
        wdl6 = R"""
        version development
        workflow count_dir {
            input {
                Directory dir
            }
            call directory_files {
                input:
                    dir = dir
            }
            output {
                Int file_count = length(directory_files.files)
            }
        }
        task directory_files {
            input {
                Directory dir
            }
            command {
                find "~{dir}" -type f > files.txt
                >&2 cat files.txt
            }
            output {
                Array[File] files = read_lines("files.txt")
            }
        }
        """

        # uncached
        inp = {"dir": "s3://1000genomes/phase3/integrated_sv_map/supporting/breakpoints/"}
        outp = self._run(wdl6, inp, task="directory_files")
        self.assertEqual(len(outp["files"]), 2)

        outp = self._run(wdl6, inp)
        self.assertEqual(outp["file_count"], 2)
        logs = [str(record.msg) for record in capture.records]

        # cached
        cfg = WDL.runtime.config.Loader(logging.getLogger(self.id()))
        cfg.override({
            "download_cache": {
                "put": True,
                "get": True,
                "dir": os.path.join(self._dir, "cache6")
            },
            "logging": { "json": True }
        })
        self._run(wdl6, inp, cfg=cfg)
        new_logs = [str(record.msg) for record in capture.records][len(logs):]
        assert "'downloaded': 1" in next(msg for msg in new_logs if "processed input URIs" in msg), str(logs)
        logs += new_logs
        self._run(wdl6, inp, cfg=cfg)
        new_logs = [str(record.msg) for record in capture.records][len(logs):]
        assert next((msg for msg in new_logs if "found in download cache" in msg), False)
        logs += new_logs
        outp = self._run(wdl6, inp, task="directory_files", cfg=cfg)
        self.assertEqual(len(outp["files"]), 2)
        new_logs = [str(record.msg) for record in capture.records][len(logs):]
        assert next((msg for msg in new_logs if "found in download cache" in msg), False)
        logs += new_logs

    @log_capture()
    def test_gsutil_directory(self, capture):
        wdl6 = R"""
        version development
        workflow count_dir {
            input {
                Directory dir
            }
            call directory_files {
                input:
                    dir = dir
            }
            output {
                Int file_count = length(directory_files.files)
            }
        }
        task directory_files {
            input {
                Directory dir
            }
            command {
                find "~{dir}" -type f > files.txt
                >&2 cat files.txt
            }
            output {
                Array[File] files = read_lines("files.txt")
            }
        }
        """

        # uncached
        inp = {"dir": "gs://genomics-public-data/ftp-trace.ncbi.nih.gov/1000genomes/ftp/phase3/integrated_sv_map/supporting/breakpoints/"}
        outp = self._run(wdl6, inp, task="directory_files")
        self.assertEqual(len(outp["files"]), 4)

        outp = self._run(wdl6, inp)
        self.assertEqual(outp["file_count"], 4)
        logs = [str(record.msg) for record in capture.records]

        # cached
        cfg = WDL.runtime.config.Loader(logging.getLogger(self.id()))
        cfg.override({
            "download_cache": {
                "put": True,
                "get": True,
                "dir": os.path.join(self._dir, "cache6")
            },
            "logging": { "json": True }
        })
        self._run(wdl6, inp, cfg=cfg)
        new_logs = [str(record.msg) for record in capture.records][len(logs):]
        assert "'downloaded': 1" in next(msg for msg in new_logs if "processed input URIs" in msg), str(logs)
        logs += new_logs
        self._run(wdl6, inp, cfg=cfg)
        new_logs = [str(record.msg) for record in capture.records][len(logs):]
        assert next((msg for msg in new_logs if "found in download cache" in msg), False)
        logs += new_logs
        outp = self._run(wdl6, inp, task="directory_files", cfg=cfg)
        self.assertEqual(len(outp["files"]), 4)
        new_logs = [str(record.msg) for record in capture.records][len(logs):]
        assert next((msg for msg in new_logs if "found in download cache" in msg), False)
        logs += new_logs

class RuntimeOverride(RunnerTestCase):
    def test_runtime_override(self):
        wdl = """
        version development
        workflow w {
            input {
                String who
            }
            call t {
                input:
                    who = who
            }
        }
        task t {
            input {
                String who
            }
            command {
                cp /etc/issue issue
                echo "Hello, ~{who}!"
            }
            output {
                String msg = read_string(stdout())
                String issue = read_string("issue")
            }
            runtime {
                docker: "ubuntu:20.04"
            }
        }
        """
        outp = self._run(wdl, {
            "who": "Alice",
            "t.runtime.container": ["ubuntu:20.10"]
        })
        assert "20.10" in outp["t.issue"]


class MiscRegressionTests(RunnerTestCase):
    def test_repeated_file_rewriting(self):
        wdl = """
        version 1.0
        task t {
            input {
                Array[File] files
            }
            command <<<
                xargs cat < ~{write_lines(files)}
                echo Bob > bob.txt
            >>>
            output {
                Array[String] out = read_lines(stdout())
                File bob = "bob.txt"
                Array[File] bob2 = [bob, bob]
            }
        }
        workflow w {
            input {
                File file
            }
            call t {
                input:
                files = [file, file]
            }
        }
        """
        with open(os.path.join(self._dir, "alice.txt"), "w") as alice:
            print("Alice", file=alice)
        outp = self._run(wdl, {"file": os.path.join(self._dir, "alice.txt")})
        self.assertEqual(outp["t.out"], ["Alice", "Alice"])

    def test_weird_filenames(self):
        chars = [c for c in (chr(i) for i in range(1,256)) if c not in ('/')]
        filenames = []
        for c in chars:
            if c != '.':
                filenames.append(c)
            filenames.append(c + ''.join(random.choices(chars,k=11)))
        assert filenames == list(sorted(filenames))
        if platform.system() == "Darwin":  # macOS is case-insensitive
            filenames = list(set(fn.lower() for fn in filenames))
        filenames.append('ThisIs{{AVeryLongFilename }}abc...}}xzy1234567890!@{{నేనుÆды.test.ext')

        inputs = {"files": []}
        for fn in filenames:
            fn = os.path.join(self._dir, fn)
            with open(fn, "w") as outfile:
                print(fn, file=outfile)
            inputs["files"].append(fn)

        wdl = """
        version development
        workflow w {
            input {
                Array[File] files
            }
            call t {
                input:
                files = files
            }
            output {
                Array[File] files_out = t.files_out
            }
        }

        task t {
            input {
                Array[File] files
            }
            command <<<
                set -euxo pipefail
                mkdir files_out
                find _miniwdl_inputs -type f -print0 | xargs -0 -iXXX cp XXX files_out/
            >>>
            output {
                Array[File] files_out = glob("files_out/*")
            }
            runtime {
                container: ["ubuntu:20.04"]
            }
        }
        """

        outp = self._run(wdl, inputs)
        outp_filenames = list(sorted(os.path.basename(fn) for fn in outp["files_out"]))
        # glob will exclude dotfiles
        expctd_filenames = list(bn for bn in sorted(os.path.basename(fn) for fn in inputs["files"]) if not bn.startswith("."))
        self.assertEqual(outp_filenames, expctd_filenames)
        euid = os.geteuid()
        for fn in outp["files_out"]:
            assert os.stat(fn).st_uid == euid

    def test_placeholder_regex(self):
        wdl = """
        version 1.1
        task vulnerable {
            input {
                String s
            }
            command <<<
                echo 'Hello, ~{s}'
            >>>
            output {
                String out = read_string(stdout())
            }
        }
        """
        self.assertEqual(self._run(wdl, {"s": "Alice"})["out"], "Hello, Alice")
        malicious = "'; exit 42; echo '"
        self._run(wdl, {"s": malicious}, expected_exception=WDL.runtime.CommandFailed)
        cfg = WDL.runtime.config.Loader(logging.getLogger(self.id()), [])
        cfg.override({"task_runtime": {"placeholder_regex": "[^']*"}})
        self._run(wdl, {"s": malicious}, cfg=cfg, expected_exception=WDL.Error.InputError)
        cfg.override({"task_runtime": {"placeholder_regex": "[0-9A-Za-z:/._-]*"}})
        self._run(wdl, {"s": malicious}, cfg=cfg, expected_exception=WDL.Error.InputError)

    def test_syntax_error(self):
        doc = r"""
        workflow wf {
            String s = "\uvwxyz"
        }
        """
        self._run(doc, {}, expected_exception=WDL.Error.SyntaxError)

    def test_spec_select_all_wdl(self):
        doc = r"""
        version 1.1
        workflow SelectAll {
            input {
                Int? maybe_five = 5
                Int? maybe_four_but_is_not = None
                Int? maybe_three = 3
            }
            output {
                Array[Int] fivethree = select_all([maybe_five, maybe_four_but_is_not, maybe_three])
                Boolean is_true = fivethree == [5, 3]
            }
        }
        """
        outp = self._run(doc, {})
        self.assertEqual(outp["fivethree"], [5, 3])
        self.assertEqual(outp["is_true"], True)

    def test_issue596(self):
        self._run("""
        task reference_prepare {
            input {
                # You need to define either this...
                File? reference_fa_file

                # Or both of these.
                File?   reference_zipped_directory
                String? reference_fa_filename_in_zipped_directory
            }

            # get the basename of the reference
            String? basename_reference = basename(reference_zipped_directory)

            command <<<
                set -eux -o pipefail

                if [[ ! "~{reference_zipped_directory}" = "" ]]
                then
                    cp ~{reference_zipped_directory} .
                    unzip ~{basename_reference}
                fi

                # do other things here
            >>>
        }""", {}, expected_exception=WDL.Error.StaticTypeMismatch)

    def test_issue614(self):
        wdl = r"""
        version 1.0
        task create_file_or_not {
            input {
                Boolean create_file
            }
            command <<<
                ~{true="touch" false="echo" create_file} file
            >>>
            output {
            File? out = "file"
            # This is common when there are a lot of (optional) files output by a
            # task. Just capture them in one variable as an array, so you can easily
            # delegate to workflow outputs.
            Array[File] all_output = select_all([out])
            }
        }
        workflow maybe_file {
            input {
                Boolean create_file = false
            }
            call create_file_or_not {
                input:
                create_file = create_file
            }
            output {
            Array[File] out = create_file_or_not.all_output
            }
        }
        """
        outp = self._run(wdl, {})
        self.assertEqual(outp["out"], [])
        outp = self._run(wdl, {"create_file": True})
        self.assertTrue(os.path.exists(outp["out"][0]))

    def test_issue696(self):
        wdl = r"""
        version 1.0
        task issue696 {
            command <<<
                touch file1
            >>>
            output {
                Array[File?] files = select_all(["file1","file2"])
            }
        }
        """
        outp = self._run(wdl, {})
        """
        This is a corner case of the treatment of File? outputs from tasks, where the correct
        answer isn't crystal clear. Specifically in task outputs, File? outputs referring to
        nonexistent container paths implicitly evaluate to None instead of raising. But when
        precisely should that conversion occur?
        
        Currently, the literal argument to select_all() above evaluates to an Array[String], and
        with no special File? treatment select_all() naturally passes through an equivalent
        length-2 array. THEN, we coerce it to Array[File?] and convert the second element to None.

        But are we supposed to have coerced the literal strings to File? -before- further
        evaluating the expression? If so, then we should only get a length-1 array.
        """
        self.assertEqual(len(outp["files"]), 2)

    @unittest.expectedFailure # issue #623
    def test_output_symlink_to_input(self):
        wdl = r"""
        version 1.0
        task create_file {
            input {}
            command <<<
                echo hello > hello.txt
            >>>
            output {
                File file = "hello.txt"
            }
        }
        task use_file {
            input {
                File file_in
            }
            command <<<
                ln -s ~{file_in} hello_link.txt
                >&2 ls -lR
            >>>
            output {
                File file = "hello_link.txt"
            }
        }
        workflow maybe_file {
            input {}
            call create_file
            call use_file {
                input:
                file_in = create_file.file
            }
            output {
                Array[File] files = [create_file.file, use_file.file]
            }
        }
        """
        outp = self._run(wdl, {})

    def test_issue699(self):
        wdl = r"""
        version 1.1

        struct S1 {
            String f1
            Array[Int] f2
        }
        struct S2 {
            String f1
            Array[Int] f2
        }

        workflow wf {
            input {}
            Array[Array[Int]] a = [[1,2,3]]
            S1 s1 = S1 {
                f1: "hello",
                f2: [42]
            }
            S2 s2 = S2 {
                f1: "hello",
                f2: [42]
            }
            S2 s3 = S2 {
                f1: "hello",
                f2: []
            }
            output {
                Array[Array[Int]] a_transposed = transpose(a)
                Array[Array[Int]] a_expected = [[1],[2],[3]]
                Boolean equal = a_transposed == a_expected

                Array[Boolean] structs_equal = [
                    s1 == s1,
                    s1 == s2,
                    s2 == s1,
                    s1 == s3,
                    s3 == s2
                ]
            }
        }"""
        outp = self._run(wdl, {})
        self.assertEqual(outp["equal"], True)
        self.assertEqual(outp["structs_equal"], [True, True, True, False, False])

    def test_issue740(self):
        wdl = r"""
        version development
        task echo {
            command <<<
                echo '{}' > out.json
            >>>

            output {
                Map[String, String]? out = if true then read_json("out.json") else None
                Int? out2 = if true then None else None
                Array[Map[String,String]] out3 = [read_json("out.json")]
                Array[Int?] out4 = [None,None]
                Array[Map[String,String]?] out5 = [if true then read_json("out.json") else None]
            }
        }
        """
        outp = self._run(wdl, {})
        self.assertEqual(outp["out"], {})
        self.assertEqual(outp["out2"], None)
        self.assertEqual(outp["out3"], [{}])
        self.assertEqual(outp["out4"], [None, None])
        self.assertEqual(outp["out5"], [{}])

class TestInlineDockerfile(RunnerTestCase):
    @log_capture()
    def test1(self, capture):
        wdl = """
        version development
        workflow w {
            call t
        }
        task t {
            input {
                Array[String]+ apt_pkgs
                Float timestamp
            }
            command <<<
                set -euxo pipefail
                apt list --installed | tr '/' $'\t' | sort > installed.txt
                sort "~{write_lines(apt_pkgs)}" > expected.txt
                join -j 1 -v 2 installed.txt expected.txt > missing.txt
                if [ -s missing.txt ]; then
                    >&2 cat missing.txt
                    exit 1
                fi
            >>>
            runtime {
                inlineDockerfile: <<<
                    FROM ubuntu:20.04
                    RUN apt-get -qq update && apt-get install -y ~{sep(' ', apt_pkgs)}
                    RUN touch ~{timestamp}
                >>>
                maxRetries: 1
            }
        }
        """
        t = time.time()  # to ensure the image is built anew on every test run
        self._run(wdl, {"t.apt_pkgs": ["samtools", "tabix"], "t.timestamp": t})
        self._run(wdl, {"t.apt_pkgs": ["samtools", "tabix"], "t.timestamp": t})
        logs = [str(record.msg) for record in capture.records if str(record.msg).startswith("docker build cached")]
        self.assertEqual(len(logs), 1)
        self._run(wdl, {"t.apt_pkgs": ["bogusfake123"], "t.timestamp": t}, expected_exception=docker.errors.BuildError)


class TestAbbreviatedCallInput(RunnerTestCase):

    def test_docker(self):
        caller = R"""
        version 1.1
        workflow caller {
            input {
                String message
                String docker
            }
            call contrived as contrived1 {
                input:
                message = "~{message}1",
                docker
            }
            call contrived as contrived2 {
                input:
                message = "~{message}2",
                docker
            }
            output {
                Array[String] results = [contrived1.result, contrived2.result]
            }
        }
        task contrived {
            input {
                String message
                String docker
            }
            command <<<
                echo "~{message}"
                cat /etc/issue
            >>>
            output {
                String result = read_string(stdout())
            }
            runtime {
                docker: docker
            }
        }
        """
        outputs = self._run(caller, {"message": "hello", "docker": "ubuntu:bionic"})
        assert sum("18.04" in msg for msg in outputs["results"]) == 2
        outputs = self._run(caller, {"message": "hello", "docker": "ubuntu:focal"})
        assert sum("20.04" in msg for msg in outputs["results"]) == 2


class TestImplicitlyOptionalInputWithDefault(RunnerTestCase):
    def test_workflow(self):
        src = R"""
        version 1.1
        workflow contrived {
            input {
                String a = "Alice" + select_first([b, "Carol"])
                String? b = "Bob"
            }
            output {
                Array[String?] results = [a, b]
            }
        }
        """
        outp = self._run(src, {})
        self.assertEqual(outp["results"], ["AliceBob", "Bob"])
        outp = self._run(src, {"a": "Alyssa"})
        self.assertEqual(outp["results"], ["Alyssa", "Bob"])
        outp = self._run(src, {"b": "Bas"})
        self.assertEqual(outp["results"], ["AliceBas", "Bas"])
        outp = self._run(src, {"b": None})
        self.assertEqual(outp["results"], ["AliceCarol", None])
        outp = self._run(src, {"a": None, "b": None})
        self.assertEqual(outp["results"], ["AliceCarol", None])

    def test_task(self):
        caller = R"""
        version 1.1
        workflow caller {
            input {
                String? a
                String? b
            }
            call contrived {
                input:
                a = a, b = b
            }
            output {
                Array[String?] results = contrived.results
            }
        }
        task contrived {
            input {
                String a = "Alice" + select_first([b, "Carol"])
                String? b = "Bob"
            }
            command {}
            output {
                Array[String?] results = [a, b]
            }
        }
        """
        outp = self._run(caller, {})
        self.assertEqual(outp["results"], ["AliceCarol", None])
        outp = self._run(caller, {"a": None, "b": None})
        self.assertEqual(outp["results"], ["AliceCarol", None])
        outp = self._run(caller, {"b": "Bas"})
        self.assertEqual(outp["results"], ["AliceBas", "Bas"])
        outp = self._run(caller, {"a": "Alyssa"})
        self.assertEqual(outp["results"], ["Alyssa", None])


class TestPassthruEnv(RunnerTestCase):
    def test1(self):
        wdl = """
        version development
        task t {
            input {
                String k1
            }
            command <<<
                echo ~{k1}
                echo "$TEST_ENV_VAR"
                echo "$SET_ENV_VAR"
                echo "$NOT_PASSED_IN_VAR"
            >>>
            output {
                String out = read_string(stdout())
            }
            runtime {
                docker: "ubuntu:20.04"
            }
        }
        """
        cfg = WDL.runtime.config.Loader(logging.getLogger(self.id()), [])
        cfg.override({"task_runtime": {"env": {"TEST_ENV_VAR": None, "SET_ENV_VAR": "set123"}}})
        with open(os.path.join(self._dir, "Alice"), mode="w") as outfile:
            print("Alice", file=outfile)
        out = self._run(wdl, {"k1": "stringvalue"}, cfg=cfg)
        self.assertEqual(out["out"], """stringvalue

set123
""",
        )
        env = {
            "TEST_ENV_VAR": "passthru_test_success",
            "NOT_PASSED_IN_VAR": "this shouldn't be passed in",
        }
        with patch.dict("os.environ", env):
            out = self._run(wdl, {"k1": "stringvalue"}, cfg=cfg)
        self.assertEqual(
            out["out"],
            """stringvalue
passthru_test_success
set123
""",
        )

class TestDockerNetwork(RunnerTestCase):
    @classmethod
    def setUpClass(self):
        super().setUpClass()
        self.subnet = "192.168.99.0/24"
        self.network_name = "miniwdl_test7_net"
        self.client = docker.from_env(version="auto")
        try:
            self.network = self.client.networks.get(self.network_name)
        except docker.errors.NotFound:
            ipam_pool = docker.types.IPAMPool(
                subnet=self.subnet,
            )
            ipam_config = docker.types.IPAMConfig(pool_configs=[ipam_pool])
            # We need a swarm-compatible docker network.
            self.network = self.client.networks.create(
                self.network_name, "overlay", ipam=ipam_config
            )


    @classmethod
    def tearDownClass(self):
        super().tearDownClass()
        self.network.remove()
        self.client.close()


    def test_network_default(self):
        wdl = """
        version development
        task t {
            command <<<
                hostname -I
            >>>
            output {
                String out = read_string(stdout())
            }
            runtime {
                docker: "ubuntu:20.04"
            }
        }
        """
        cfg = WDL.runtime.config.Loader(logging.getLogger(self.id()), [])
        cfg.override(
            {"task_runtime": {"defaults": {"docker_network": self.network_name}},
             "docker_swarm": {"allow_networks": [self.network_name]}}
        )
        out = self._run(wdl, {}, cfg=cfg)
        self.assertEqual(out["out"][:11], "192.168.99.")


    @log_capture()
    def test_network_explicit(self, capture):
        wdl = f"""
        version development
        task t {{
            command <<<
                hostname -I
            >>>
            output {{
                String out = read_string(stdout())
            }}
            runtime {{
                docker_network: "{self.network_name}"
                docker: "ubuntu:20.04"
            }}
        }}
        """
        cfg = WDL.runtime.config.Loader(logging.getLogger(self.id()), [])
        cfg.override(
            {"docker_swarm": {"allow_networks": [self.network_name]}}
        )
        out = self._run(wdl, {}, cfg=cfg)
        self.assertEqual(out["out"][:11], "192.168.99.")

        # make sure allowlist is effective
        out = self._run(wdl, {})
        self.assertNotEqual(out["out"][:11], "192.168.99.")
        logs = "\n".join(str(record.msg) for record in capture.records)
        assert "runtime.docker_network ignored" in logs


    def test_network_host(self):
        wdl = """
        version development
        task t {
            command <<<
                hostname -I
            >>>
            output {
                String out = read_string(stdout())
            }
            runtime {
                docker: "ubuntu:20.04"
                docker_network: "host"
            }
        }
        """
        cfg = WDL.runtime.config.Loader(logging.getLogger(self.id()), [])
        cfg.override(
            {"docker_swarm": {"allow_networks": ["host"]}}
        )
        self._run(wdl, {}, cfg=cfg)


class TestRelativeOutputPaths(RunnerTestCase):
    """
    More tests for this feature are in runner.t. This one is for basic coverage.
    """
    wdl = """
    version development
    workflow w {
        input {
            Array[String] names
        }
        scatter (name in names) {
            call t {
                input: name
            }
        }
        output {
            Array[File] messages = t.message
        }
    }
    task t {
        input {
            String name
        }
        command <<<
            mkdir out
            echo "Hello, ~{name}]" > 'out/~{name}.txt'
        >>>
        output {
            File message = "out/~{name}.txt"
        }
    }
    """

    def test_ok(self):
        cfg = WDL.runtime.config.Loader(logging.getLogger(self.id()), [])
        cfg.override({"file_io": {"use_relative_output_paths": True}})
        outp = self._run(self.wdl, {"names": ["Alyssa", "Ben"]}, cfg=cfg)
        self.assertTrue(outp["messages"][0].endswith("/out/out/Alyssa.txt"))
        self.assertTrue(outp["messages"][1].endswith("/out/out/Ben.txt"))

    def test_collision(self):
        cfg = WDL.runtime.config.Loader(logging.getLogger(self.id()), [])
        cfg.override({"file_io": {"use_relative_output_paths": True}})
        with self.assertRaises(WDL.runtime.error.RunFailed):
            self._run(self.wdl, {"names": ["Ben", "Ben"]}, cfg=cfg)


class TestEnvDecl(RunnerTestCase):
    def test_basic(self):
        outp = self._run("""
            version development
            workflow w {
                scatter (who in ["Alyssa", "Ben"]) {
                    call t { input: who }
                }
                output {
                    Array[String] messages = t.message
                }
            }
            task t {
                input {
                    env String who
                    String non_env = "XXX"
                }
                env String greeting = "Hello"
                String non_env2 = "YYY"
                command <<<
                    echo "${greeting}, $who!${non_env:-}${non_env2:-}" | tee /dev/stderr
                >>>
                output {
                    String message = read_string(stdout())
                }
            }
        """, {})
        assert outp["messages"] == ["Hello, Alyssa!", "Hello, Ben!"]

    def test_more(self):
        with open(os.path.join(self._dir, "alyssa.txt"), mode="w") as outfile:
            print("Alyssa", file=outfile)
        outp = self._run("""
            version development
            struct Person {
                File name
                Int age
            }
            task t {
                input {
                    env Person p
                }
                env File name = p.name
                command <<<
                    echo "Hello, $(cat "$name")!" | tee /dev/stderr
                    echo "$p" | tr -d ' ' | grep '"age":42' >&2
                >>>
                output {
                    String message = read_string(stdout())
                }
            }
        """, {"p": {"name": os.path.join(self._dir, "alyssa.txt"), "age": 42}})
        assert outp["message"] == "Hello, Alyssa!"

class TestNestedInterpolations(RunnerTestCase):
    wdl = """
        workflow w {
            String a = "Alice"
            Array[String] bc = ["Bob", "Carol"]
            String sp = " "

            call t

            output {
                String message = "Hello ${a + "${sp}"}!"
                String task_out = t.out
            }
        } 
        task t {
            input {}
            String a = "Alice"
            Array[String] bc = ["Bob", "Carol"]
            String sp = " "

            command <<<
                echo 'Hello ~{a + "~{sp}~{sep=' ' bc}"}!'
            >>>

            output {
                String out = read_string(stdout())
            }
        }
    """

    def test_all(self):
        for version in ("1.1", "development"):
            outp = self._run(f"version {version}\n" + self.wdl, {})
            assert outp["message"] == "Hello Alice !"
            assert outp["task_out"].strip() == "Hello Alice Bob Carol!"
