"""diff: the Delta that turns one retained snapshot into another."""

from lumewarp_control_plane._proto.lumewarp.config.v1 import config_service_pb2, snapshot_pb2

# (Snapshot field, Delta upsert field, Delta remove field, entity key)
ENTITIES = (
    ("upstreams", "upsert_upstreams", "remove_upstreams", "name"),
    ("deployments", "upsert_deployments", "remove_deployments", "name"),
    ("routes", "upsert_routes", "remove_routes", "model"),
    ("api_keys", "upsert_api_keys", "remove_api_keys", "key_id"),
    ("teams", "upsert_teams", "remove_teams", "id"),
)


def diff(old: snapshot_pb2.Snapshot, new: snapshot_pb2.Snapshot) -> config_service_pb2.Delta:
    """Delta that turns `old` into `new` (base_version=old.version, version=new.version).

    Upserts carry whole entities keyed by name / key_id / team id, in `new`'s order;
    removes list keys that disappeared, sorted; created_at_unix_ms and
    required_capabilities are copied from `new`. Applying the result to `old` (removes,
    then upserts by key) yields a snapshot equal to `new` up to entity order, which the
    compiler keeps sorted.
    """
    if old.scope != new.scope:
        raise ValueError(f"cannot diff across scopes ({old.scope!r} -> {new.scope!r})")
    delta = config_service_pb2.Delta(
        base_version=old.version,
        version=new.version,
        created_at_unix_ms=new.created_at_unix_ms,
        required_capabilities=list(new.required_capabilities),
    )
    for field, upsert_field, remove_field, key in ENTITIES:
        before = {getattr(e, key): e for e in getattr(old, field)}
        after_keys = set()
        upserts = getattr(delta, upsert_field)
        for entity in getattr(new, field):
            k = getattr(entity, key)
            after_keys.add(k)
            if before.get(k) != entity:
                upserts.append(entity)
        getattr(delta, remove_field).extend(sorted(set(before) - after_keys))
    return delta
