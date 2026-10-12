ALTER TABLE session_entries ADD COLUMN confirmation TEXT
    CHECK (confirmation IS NULL OR (type = 'message' AND json_valid(confirmation)
        AND json_extract(messages, '$[0].role') = 'user'));
