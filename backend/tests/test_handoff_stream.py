"""B4: change stream de agent_handoffs — filtro no servidor, retomada, resync e limite de assinantes."""

import asyncio

import pytest

from app import main
from app.database import DataStore
from app.config import Settings


def test_pipeline_filters_owner_on_server_without_legacy_branch():
    pipe = DataStore.handoff_pipeline("ana")
    match = pipe[0]["$match"]
    assert {"fullDocument.customer_key": "ana"} in match["$or"]
    assert "$exists" not in str(pipe)          # o ramo legado sem dono saiu
    assert match["operationType"]["$in"] == ["insert", "invalidate"]


class _Stream:
    def __init__(self, events, fail=None, token=None):
        self.events, self.fail, self.closed, self.resume_token = list(events), fail, False, token

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if self.events:
            return self.events.pop(0)
        if self.fail:
            exc, self.fail = self.fail, None
            raise exc
        await asyncio.sleep(3600)

    async def close(self):
        self.closed = True


class _Lost(Exception):
    code = 286


class _Drop(Exception):
    pass


def _evt(n, key="ana"):
    return {"_id": {"_data": f"t{n}"}, "operationType": "insert",
            "fullDocument": {"_id": n, "customer_key": key, "conversation_id": "c", "to_agent": "x"}}


class _Coll:
    def __init__(self, streams):
        self.streams, self.opened = iter(streams), []

    async def watch(self, pipeline, **kw):
        self.opened.append(kw.get("resume_after"))
        nxt = next(self.streams)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _store(coll):
    store = DataStore(Settings(demo_mode=False, mongodb_uri=""))
    store.memory = False
    store._collection = lambda name, brain=False: coll
    return store


def _collect(coll, n, key="ana"):
    async def go():
        out = []
        agen = _store(coll)._watch_handoffs_atlas(key)
        async for item in agen:
            out.append(item)
            if len(out) == n:
                break
        await agen.aclose()
        return out
    return asyncio.run(asyncio.wait_for(go(), 10))


def test_resumes_with_token_after_drop():
    pytest.importorskip("atlas_retrieval")
    coll = _Coll([_Stream([_evt(1)], fail=_Drop("conexão caiu")), _Stream([_evt(2)])])
    items = _collect(coll, 2)
    assert [i["_id"] if "_id" in i else i.get("to_agent") for i in items] and len(items) == 2
    assert coll.opened == [None, {"_data": "t1"}]


def test_history_lost_becomes_resync_event():
    pytest.importorskip("atlas_retrieval")
    coll = _Coll([_Lost("ChangeStreamHistoryLost"), _Stream([_evt(3)])])
    items = _collect(coll, 2)
    assert "_resync" in items[0] and items[1]["to_agent"] == "x"


def test_out_of_scope_document_is_dropped_defensively():
    pytest.importorskip("atlas_retrieval")
    coll = _Coll([_Stream([_evt(1, key="bruno"), _evt(2, key="ana")])])
    items = _collect(coll, 1)
    assert len(items) == 1 and items[0]["customer_key"] == "ana"


def test_subscriber_limits_and_idempotent_release(monkeypatch):
    monkeypatch.setattr(main, "MAX_HANDOFF_SUBSCRIBERS", 3)
    monkeypatch.setattr(main, "MAX_HANDOFF_SUBSCRIBERS_PER_CUSTOMER", 2)
    main._handoff_subscribers.clear()
    assert main._subscribe("ana") and main._subscribe("ana")
    assert not main._subscribe("ana")              # teto por cliente
    assert main._subscribe("bruno")
    assert not main._subscribe("carla")            # teto global
    sub = main._Subscription("ana")
    sub.release()
    sub.release()                                  # idempotente
    assert main._handoff_subscribers == {"ana": 1, "bruno": 1}
    main._handoff_subscribers.clear()


def test_empty_or_non_string_customer_never_opens_stream():
    async def go(key):
        store = DataStore(Settings(demo_mode=True))
        await store.connect()
        return [item async for item in store.watch_handoffs(key)]
    for bad in ("", None, {"$ne": None}):
        assert asyncio.run(go(bad)) == []
