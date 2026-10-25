"""Owned subprocess transport failures, with synthetic content-free fixtures."""
import io
import os
from pathlib import Path
import sys
import textwrap

import numpy as np
import pytest

from whisperstream.speech.kokoro_process import KokoroProcess, worker_command
from whisperstream.speech.kokoro_worker import run_worker
from whisperstream.stt_worker_protocol import read_frame, write_frame


@pytest.fixture
def child_command(tmp_path):
    script = tmp_path / "synthetic-child.py"
    script.write_text(textwrap.dedent('''
        import os, struct, sys, time
        import numpy as np
        from whisperstream.stt_worker_protocol import read_frame, write_frame
        source, target = sys.stdin.buffer, sys.stdout.buffer
        write_frame(target, {"event":"ready", "pid":os.getpid()})
        load = read_frame(source)
        mode = load.header["directory"]
        if mode == "load-timeout": time.sleep(30)
        write_frame(target, {"event":"loaded", "sample_rate":24000, "num_speakers":54})
        while True:
            frame=read_frame(source)
            if mode == "exit": os._exit(7)
            if mode == "eof": sys.exit(0)
            if mode == "timeout": time.sleep(30)
            if mode == "malformed":
                target.write(b"XXXX"+struct.pack(">II",2,0)+b"{}"); target.flush(); time.sleep(30)
            if mode == "oversized":
                write_frame(target,{"event":"chunk","progress":1.0},b"x"*8000004); time.sleep(30)
            if mode == "failed":
                print("synthetic secret native diagnostic",file=sys.stderr,flush=True)
                write_frame(target,{"event":"failed"}); time.sleep(30)
            if mode == "empty":
                write_frame(target,{"event":"done"}); continue
            for progress in (0.5,1.0):
                write_frame(target,{"event":"chunk","progress":progress},np.array([0.1,-0.1],dtype="<f4").tobytes())
                ack=read_frame(source)
                if not ack.header["continue"]: break
            write_frame(target,{"event":"done"})
    '''))
    return [sys.executable, str(script)]


def test_owned_worker_continuation_stop_and_cleanup(child_command):
    worker = KokoroProcess(Path("ok"), 2, command=child_command)
    try:
        calls = []
        audio = worker.generate("Synthetic input.", sid=3, speed=1,
                                callback=lambda a,p: calls.append((a.size,p)) or 1)
        assert calls == [(2,0.5),(2,1.0)]
        assert audio.samples.size == 4
        calls.clear()
        audio = worker.generate("Synthetic input.", sid=3, speed=1,
                                callback=lambda a,p: calls.append(p) or 0)
        assert calls == [0.5]
        assert audio.samples.size == 2
    finally:
        worker.close()
    assert worker._process.poll() is not None
    assert not worker._reader.is_alive()
    worker.close()


@pytest.mark.parametrize("mode", ["exit", "eof", "timeout", "malformed", "oversized", "failed"])
def test_failures_retire_exact_child_without_native_content(child_command, mode, capfd):
    worker = KokoroProcess(Path(mode), 2, command=child_command, generate_timeout=0.3)
    calls = []
    with pytest.raises(RuntimeError) as error:
        worker.generate("Synthetic input.", sid=3, speed=1, callback=lambda *_: calls.append(1) or 1)
    assert "synthetic secret" not in str(error.value)
    assert calls == []
    assert worker._process.poll() is not None
    assert not worker._reader.is_alive()
    output = capfd.readouterr()
    assert "synthetic secret" not in output.out + output.err


def test_callback_exception_retires_child(child_command):
    worker = KokoroProcess(Path("ok"), 2, command=child_command)
    calls = []
    def callback(*_):
        calls.append(1)
        raise ValueError("synthetic callback failure")
    with pytest.raises(ValueError, match="callback failure"):
        worker.generate("Synthetic input.", sid=3, speed=1, callback=callback)
    assert calls == [1]
    assert not worker.alive
    assert not worker._reader.is_alive()


def test_empty_native_outcome_is_not_reported_as_speech(child_command, monkeypatch):
    from whisperstream.speech.kokoro_engine import KokoroEngine
    monkeypatch.setattr("whisperstream.speech.availability.require_engine_available",lambda _: None)
    process=KokoroProcess(Path("empty"),2,command=child_command)
    engine=KokoroEngine()
    engine._engine=process
    try:
        with pytest.raises(RuntimeError,match="invalid audio"):
            engine.generate("Synthetic failed input.")
    finally:
        engine.unload()


def test_startup_timeout_closes_process(child_command):
    with pytest.raises(RuntimeError, match="timed out"):
        KokoroProcess(Path("load-timeout"),2,command=child_command,load_timeout=0.5)


def test_cleanup_after_onefile_launcher_exits(child_command, tmp_path):
    launcher=tmp_path/"synthetic-launcher.py"
    launcher.write_text(textwrap.dedent('''
        import os, subprocess, sys, time
        subprocess.Popen(sys.argv[1:],stdin=sys.stdin,stdout=sys.stdout,stderr=sys.stderr)
        time.sleep(1.0)
        os._exit(0)
    '''))
    worker=KokoroProcess(Path("ok"),2,command=[sys.executable,str(launcher),*child_command])
    runtime=worker._runtime_identity
    assert runtime.pid != worker._process.pid
    worker._process.wait(timeout=3)
    assert runtime.is_running()
    worker.close()
    assert not runtime.is_running()
    assert not worker._reader.is_alive()


def test_worker_exception_and_eof_do_not_expose_exception_contents():
    source, target = io.BytesIO(), io.BytesIO()
    write_frame(source, {"operation":"load","directory":"synthetic", "threads":2})
    source.seek(0)
    def fail(*_):
        raise ValueError("synthetic secret must stay in child")
    assert run_worker(source,target,runtime_factory=fail) == 1
    target.seek(0)
    assert read_frame(target).header["event"] == "ready"
    assert read_frame(target).header == {"event":"failed"}
    assert b"synthetic secret" not in target.getvalue()
    assert run_worker(io.BytesIO(),io.BytesIO()) == 0


def test_child_native_stdout_and_stderr_cannot_corrupt_protocol(tmp_path, capfd):
    script=tmp_path/"native-diagnostics-child.py"
    script.write_text(textwrap.dedent('''
        import os
        from types import SimpleNamespace
        import numpy as np
        from whisperstream.speech import kokoro_worker
        def load(*_):
            os.write(1,b"synthetic native load output")
            os.write(2,b"synthetic native load error")
            def generate(*args,callback,**kwargs):
                os.write(1,b"synthetic native text fragment")
                os.write(2,b"synthetic native phoneme fragment")
                callback(np.array([0.1,-0.1],dtype=np.float32),1.0)
            return SimpleNamespace(sample_rate=24000,num_speakers=54,generate=generate)
        kokoro_worker._load_native=load
        raise SystemExit(kokoro_worker.main())
    '''))
    worker=KokoroProcess(Path("synthetic"),2,command=[sys.executable,str(script)])
    try:
        audio=worker.generate("Synthetic input.",sid=3,speed=1,callback=lambda *_: 1)
        assert audio.samples.size == 2
    finally:
        worker.close()
    output=capfd.readouterr()
    assert output.out == output.err == ""


def test_source_and_frozen_worker_dispatch(monkeypatch):
    from whisperstream import stt_supervisor
    monkeypatch.setattr(stt_supervisor,"default_worker_command",lambda: ("synthetic.exe","--stt-worker"))
    assert worker_command() == ("synthetic.exe","--kokoro-worker")
    root = Path(__file__).resolve().parents[1]
    for name in ("scripts/build.py","scripts/build_macos_internal.py"):
        assert "--include-module=whisperstream.speech.kokoro_worker" in (root/name).read_text(encoding="utf-8")
    import importlib.util
    spec=importlib.util.spec_from_file_location("kokoro_entry",root/"whisperstream_entry.py")
    entry=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    monkeypatch.setattr(sys,"argv",["synthetic.exe","--kokoro-worker"])
    monkeypatch.setattr("whisperstream.speech.kokoro_worker.main",lambda: 17)
    assert entry._run() == 17


@pytest.fixture
def real_wrapper(monkeypatch):
    supplied=os.environ.get("WHISPERSTREAM_KOKORO_TEST_MODEL_DIR")
    if not supplied:
        pytest.skip("Explicit pinned upstream model directory required for native synthesis")
    from whisperstream.speech import kokoro_engine, availability
    monkeypatch.setattr(availability,"require_engine_available",lambda _: None)
    monkeypatch.setattr(kokoro_engine,"is_present",lambda _: True)
    monkeypatch.setattr(kokoro_engine,"model_dir",lambda _: Path(supplied))
    engine=kokoro_engine.KokoroEngine()
    engine.load()
    yield engine
    process=engine._engine
    engine.unload()
    assert not process.alive
    assert not process._reader.is_alive()


def test_real_nine_voices_and_speeds(real_wrapper, capfd):
    for voice in real_wrapper.voice_ids:
        audio=real_wrapper.generate("A synthetic voice verification sentence.",sid=voice)
        assert audio.size > 1000 and np.isfinite(audio).all() and np.max(np.abs(audio)) > 0.001
    for speed in (0.5,2.0):
        assert real_wrapper.generate("A synthetic speed verification sentence.",speed=speed).size > 1000
    output=capfd.readouterr()
    assert output.out == output.err == ""


def test_real_interruption_and_callback_failure(real_wrapper):
    text="A synthetic first sentence. Another test sentence. A final test sentence."
    continued=[]
    real_wrapper.generate(text,callback=lambda _,p: continued.append(p) or 0)
    stopped=[]
    real_wrapper.generate(text,callback=lambda _,p: stopped.append(p) or 1)
    assert len(continued) == 3
    assert len(stopped) == 1
    process=real_wrapper._engine
    def fail(*_):
        raise ValueError("synthetic callback failure")
    with pytest.raises(ValueError):
        real_wrapper.generate(text,callback=fail)
    assert not process.alive
    real_wrapper.load()  # Explicit retry starts a fresh owned process.
    assert real_wrapper.generate("Fresh synthetic retry.").size > 1000


def test_real_native_failure_diagnostics_stay_in_child(real_wrapper, capfd):
    # Mixed CJK fragments exercise native OOV diagnostics even with debug=False.
    for text in ("Synthetic English 中文 fragment.", "Synthetic English 🙂 … fragment.",
                 "Synthetic xqzvvv $123.45 fragment."):
        audio=real_wrapper.generate(text)
        assert audio.size > 1000 and np.isfinite(audio).all()
    with pytest.raises(ValueError):
        real_wrapper.generate("🙂")
    output=capfd.readouterr()
    assert output.out == output.err == ""
