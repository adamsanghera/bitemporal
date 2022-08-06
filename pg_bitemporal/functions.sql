-- force the transaction time for the sql transaction to be a particular timestamptz
CREATE OR REPLACE FUNCTION public.set_transaction_time (tmp_fixed_time timestamptz)
    RETURNS bool
    AS $$
BEGIN
    -- this operation is meaningless if history-tracking is disabled
    IF ((
        SELECT
            to_regclass ('record_history_disabled')) IS NOT NULL) THEN
        RAISE EXCEPTION '`set_record_history` is not enabled';
    END IF;

    -- stop forcing the `fixed_transaction_time`
    IF (tmp_fixed_time IS NULL) THEN
        DROP TABLE IF EXISTS fixed_transaction_time;
        RETURN TRUE;
    END IF;

    -- create the temporary forcing-table, if it doesn't already exist
    CREATE TEMPORARY TABLE IF NOT EXISTS fixed_transaction_time (
        id int NOT NULL,
        tmp_fixed_time timestamptz NOT NULL,
        CONSTRAINT transactoin_time_pkey PRIMARY KEY (id )
    ) ON COMMIT DROP;

    -- "upsert" the forced time into the temporary table
    INSERT INTO fixed_transaction_time (id, tmp_fixed_time)
        VALUES (1, tmp_fixed_time)
    ON CONFLICT (id)
        DO UPDATE SET
            tmp_fixed_time = tmp_fixed_time;

    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;

-- This is an "application layer" enable/disable flag. While postgres functions are
-- "global" and shared by more than one transaction, this function allows us to toggle
-- the function on and off within a single transaction. It lives and dies inside the
-- transaction, and holds no global state outside the transaction.
CREATE OR REPLACE FUNCTION public.toggle_record_history (is_enabled bool)
    RETURNS bool
    AS $$
BEGIN
    -- create or drop a "flag-table" as appropriate.
    -- if the table exists, we will not track history.
    IF (is_enabled) THEN
        DROP TABLE IF EXISTS record_history_disabled;
    ELSE
        CREATE TEMPORARY TABLE IF NOT EXISTS record_history_disabled ( ) ON COMMIT DROP;
    END IF;

    RETURN TRUE;
END;
$$
LANGUAGE plpgsql;


-- `record_txn_history` turns `update` and `delete` into non-destructive mutations.
--
-- How `update` looks, executed at transaction time `X`:
-- Before:                      After:
-- Current txn_time:     [--->  Current txn_time:         [--->
-- History txn_time: [---)      History txn_time: [---[---)
--                     1   2   X                      1   2   X
--
-- How `delete` looks, executed at transaction time `X`:
-- Before:                      After:
-- Current txn_time:     [--->  Current txn_time:
-- History txn_time: [---)      History txn_time: [---[---)
--                     1   2   X                      1   2   X
--
-- This operation is simple in theory, there are some complications in practice.
--
-- * In a single transaction, "time" is frozen. So there may be competing updates that
--   occur at the same "time". In these situations, we preserve the most recent data
--   and do not write the intermediary information to the history table. This helps us
--   avoid zero-sized ranges, but is a limitation worth knowing for app clients! 
-- * Sometimes, we want to "freeze" the transaction time ourselves. Either for testing
--   reasons or for application reasons. We use a table `fixed_transaction_time` that
--   is accessible via `set_transaction_time`.
-- * When setting the old transaction period to the measured timestamp, it is possible
--   for the period to post-date the measured timestamp. In this case, we barf.
-- * It is sometimes helpful to pause history tracking, if we want to make a migration
--   without impacting history (although this is obviously discouraged). We have a
--   sentinel table, `record_history_disabled`, gated by `toggle_record_history`.
CREATE OR REPLACE FUNCTION public.record_txn_history ()
    RETURNS TRIGGER
    AS $$
DECLARE
    fixed_transaction_time_table regclass;
    measured_time timestamptz;
BEGIN
    -- validate the argument (the transaction history table)
    -- TODO: validate table exists; although we'll raise further down if it doesn't.
    IF (tg_nargs != 1) THEN
        RAISE EXCEPTION 'record_txn_history expects 1 argument'
            USING hint = 'history table name required';
    END IF;

    -- early-exit if trigger is disabled
    IF ((to_regclass ('record_history_disabled')) IS NOT NULL) THEN
        IF (tg_op = 'INSERT') THEN
            RETURN new;
        elsif (tg_op = 'UPDATE') THEN
            RETURN new;
        elsif (tg_op = 'DELETE') THEN
            RETURN old;
        ELSE
            RAISE EXCEPTION 'record_txn_history only works on insert, update, or delete';
        END IF;
    END IF;

    -- measure time from either fixed value (if set) or current_timestamp
    SELECT
        to_regclass ('fixed_transaction_time') INTO fixed_transaction_time_table;
    IF (fixed_transaction_time_table IS NOT NULL) THEN
        SELECT
            tmp_fixed_time INTO measured_time
        FROM
            fixed_transaction_time_table
        WHERE
            id = 1;
    ELSE
        measured_time := CURRENT_TIMESTAMP;
    END IF;

    -- insert: very simple! new row beginning at current time.
    -- no need to update `old`, since it does not exist.
    IF (tg_op = 'INSERT') THEN
        NEW.txn_period := tstzrange(measured_time, NULL);
        RETURN new;
    END IF;

    -- not an insert, so we may truncate `old.txn_period`
    IF (measured_time < lower(OLD.txn_period)) THEN
        RAISE EXCEPTION 'postgres time is lower than the previous txn period start'
            USING hint = '';
            -- TODO make a proper explanation
        elsif (measured_time = lower(OLD.txn_period)) THEN
            -- We don't need to persist data that's replaced in the *same DB transaction*,
            -- so we allow it to either be deleted or replaced with the latest value.
        ELSE
            -- most commoon scenario: clear space for new value's transaction period
            OLD.txn_period := tstzrange(lower(OLD.txn_period), measured_time);
            EXECUTE format('insert into %s select $1.*', TG_ARGV[0])
            USING old;
    END IF;

    -- update: basically an insert, since we've cleared `old.txn_period`
    IF (tg_op = 'UPDATE') THEN
        NEW.txn_period := tstzrange(measured_time, NULL);
        RETURN new;
    END IF;
    -- delete: no-op, since `old.txn_period` was closed earlier
    IF (tg_op = 'DELETE') THEN
        RETURN old;
    END IF;
    RAISE EXCEPTION 'record_txn_history only works on insert, update, or delete';
END;
$$ LANGUAGE plpgsql;

/* Split an existing row with some `app_period` into two non-overlapping chunks.

If the app period arguments look like:
    app_period_1: [a, b)
    app_period_2: [c, d)

It's assumed that the existing row's valid period *begins at* `a` and *ends at* `d`.
and that `a < b < c < d`. For example:

    a    b  c    d
    [-+1-)  [-+2-)
    [---`row_id`--)

The function deletes the existing record, and creates two new records with updated ids.
*/
CREATE OR REPLACE FUNCTION public.split_record(
    tmp_record ANYELEMENT,
    row_id BIGINT,
    app_period_1 TSTZRANGE,
    app_period_2 TSTZRANGE
) RETURNS BOOL AS $$
DECLARE
    found_rows INT;
BEGIN
    -- fetch the existing row
    EXECUTE format(
        'SELECT * FROM %s WHERE row_id = $1', pg_typeof(tmp_record)
    ) INTO tmp_record USING row_id;

    -- validate that the row was loaded (it could have been deleted / never existed!)
    GET DIAGNOSTICS found_rows = ROW_COUNT;
    IF found_rows != 1 THEN
        RETURN FALSE;
    END IF;

    -- delete the existing row (i.e. expire it to the history table)
    EXECUTE format(
        'DELETE FROM %s WHERE row_id = $1', pg_typeof(tmp_record)
    ) USING row_id;

    -- insert the first row, replacing `row_id` and `app_period` as appropriate
    SELECT (
        tmp_record #= hstore(
            ARRAY['row_id', 'app_period'],
            ARRAY[
                nextval(
                    pg_get_serial_sequence(
                        pg_typeof(tmp_record)::text, 'row_id'
                    )
                )::text,
                app_period_1::text
            ]
        )
    ).* INTO tmp_record;
    EXECUTE format(
        'INSERT INTO %s SELECT ($1).*', pg_typeof(tmp_record)
    ) USING tmp_record;

    -- insert the second row, replacing `row_id` and `app_period` as appropriate
    SELECT (
        tmp_record #= hstore(
            ARRAY['row_id', 'app_period'],
            ARRAY[
                nextval(
                    pg_get_serial_sequence(
                        pg_typeof(tmp_record)::text, 'row_id'
                    )
                )::text,
                app_period_2::text
            ]
        )
    ).* INTO tmp_record;
    EXECUTE format(
        'INSERT INTO %s SELECT ($1).*', pg_typeof(tmp_record)
    ) USING tmp_record;

    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;
