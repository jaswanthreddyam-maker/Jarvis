CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,
    namespace TEXT NOT NULL,
    category TEXT NOT NULL,
    content TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation (
    turn_id INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,
    user_text TEXT NOT NULL,
    assistant_text TEXT NOT NULL
);
