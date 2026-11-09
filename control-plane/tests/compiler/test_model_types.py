"""The database and compiler enforce family-specific settings independently of REST."""

import pytest
from sqlalchemy.exc import IntegrityError

from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler.compile import CompileError, compile_scope
from tests import factories
from tests.compiler.helpers import OCT, SCOPE, seed_scope


async def compile_(factory):
    async with factory() as session:
        return await compile_scope(session, SCOPE, now=OCT, require_local_membership=True)


@pytest.mark.parametrize(
    ("kind", "wire_type", "price", "caps"),
    [
        ("llm", pb.MODEL_TYPE_LLM, None, set()),
        ("embedding", pb.MODEL_TYPE_EMBEDDING, None, {"model_types"}),
        ("transcription", pb.MODEL_TYPE_TRANSCRIPTION, 123, {"model_types", "audio_price"}),
        ("transcription", pb.MODEL_TYPE_TRANSCRIPTION, 0, {"model_types"}),
        ("decision", pb.MODEL_TYPE_DECISION, None, {"model_types"}),
    ],
)
async def test_compile_type_and_price(factory, kind, wire_type, price, caps):
    await seed_scope(factory)
    async with factory() as session, session.begin():
        await factories.make_model(
            session, "typed", model_type=kind, price_nano_usd_per_audio_second=price
        )
    snap = (await compile_(factory)).snapshot
    assert snap.models[0].type == wire_type
    assert snap.models[0].price.nano_usd_per_audio_second == (price or 0)
    assert snap.models[0].HasField("price") == (price is not None)
    assert set(snap.required_capabilities) == caps


@pytest.mark.parametrize(
    "settings",
    [
        {"model_type": None},
        {
            "model_type": "embedding",
            "price_input_nano_usd_per_mtok": 1,
            "price_output_nano_usd_per_mtok": 1,
        },
        {"model_type": "embedding", "price_output_nano_usd_per_mtok": 0},
        {"model_type": "decision", "price_nano_usd_per_audio_second": 0},
        {"model_type": "transcription", "price_nano_usd_per_audio_second": -1},
        {"model_type": "transcription", "price_input_nano_usd_per_mtok": 1},
        {"model_type": "transcription", "default_body": {}},
        {"model_type": "embedding", "max_output_tokens": 1},
        {"model_type": "decision", "reasoning_mode": "strip"},
        {"model_type": "decision", "reasoning_default_level": "low"},
    ],
)
async def test_db_refuses_invalid_type_settings(session, settings):
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await factories.make_model(session, "invalid", **settings)


async def test_compiler_refuses_mixed_router(factory):
    await seed_scope(factory)
    async with factory() as session, session.begin():
        a = await factories.make_model(session, "a")
        b = await factories.make_model(session, "b", model_type="embedding")
        await factories.make_router(session, "mixed", [a, b])
    with pytest.raises(CompileError, match="share one model type"):
        await compile_(factory)
