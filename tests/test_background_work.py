"""Backpressure and retirement under stalled Agent Speak callbacks."""

from threading import Event, get_ident

from whisperstream.background_work import CoalescingPublisher, DeadlineScheduler


def test_deadline_rearming_reuses_worker_and_keeps_only_latest_call():
    scheduler = DeadlineScheduler()
    entered, release, finished = Event(), Event(), Event()
    workers, calls = [], []

    def blocked():
        workers.append(get_ident())
        entered.set()
        assert release.wait(5)

    def latest():
        workers.append(get_ident())
        calls.append("latest")
        finished.set()

    try:
        scheduler.schedule(0, blocked)
        assert entered.wait(5)
        worker = scheduler._thread
        for _ in range(1000):
            scheduler.schedule(0, lambda: calls.append("obsolete"))
        scheduler.schedule(0, latest)
        assert scheduler._thread is worker
        assert calls == []
        release.set()
        assert finished.wait(5)
        worker.join(5)
        assert not worker.is_alive()
        assert calls == ["latest"]
        assert workers[0] == workers[1]
    finally:
        release.set()
        scheduler.close()


def test_cancel_and_close_discard_deadlines_without_waiting_for_callback():
    scheduler = DeadlineScheduler()
    entered, release = Event(), Event()
    calls = []

    def blocked():
        entered.set()
        assert release.wait(5)

    try:
        scheduler.schedule(0, blocked)
        assert entered.wait(5)
        worker = scheduler._thread
        scheduler.schedule(0, lambda: calls.append("cancelled")).cancel()
        scheduler.schedule(0, lambda: calls.append("closed"))
        scheduler.close()
        scheduler.schedule(0, lambda: calls.append("after-close"))
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert calls == []
    finally:
        release.set()
        scheduler.close()


def test_publication_is_bounded_and_coalesces_to_latest_state():
    entered, release = Event(), Event()
    state = [0]
    publications = []

    def publish():
        publications.append(state[0])
        if len(publications) == 1:
            entered.set()
            assert release.wait(5)

    publisher = CoalescingPublisher(publish)
    try:
        publisher.submit()
        assert entered.wait(5)
        worker = publisher._thread
        for value in range(1, 1001):
            state[0] = value
            publisher.submit()
        assert publisher._thread is worker
        assert publications == [0]
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert publications == [0, 1000]
    finally:
        release.set()
        publisher.close()


def test_publisher_close_drops_dirty_work_and_late_submissions():
    entered, release = Event(), Event()
    calls = []

    def publish():
        calls.append(1)
        entered.set()
        assert release.wait(5)

    publisher = CoalescingPublisher(publish)
    try:
        publisher.submit()
        assert entered.wait(5)
        worker = publisher._thread
        publisher.submit()
        publisher.close()
        publisher.submit()
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert calls == [1]
    finally:
        release.set()
        publisher.close()
