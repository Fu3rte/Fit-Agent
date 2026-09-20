CREATE TABLE tool_cache_revisions (
    namespace TEXT PRIMARY KEY CHECK (namespace IN ('plans', 'workouts', 'metrics')),
    revision INTEGER NOT NULL
);

INSERT INTO tool_cache_revisions (namespace, revision) VALUES
    ('plans', 0), ('workouts', 0), ('metrics', 0);
