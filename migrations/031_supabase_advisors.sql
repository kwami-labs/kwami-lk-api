-- =============================================================================
-- What the Supabase advisors flag, fixed (2026-11-06)
--
-- Security
--   * credit_transactions_suspected_duplicates ran with its owner's rights and the
--     public roles could select it: anyone with the anon key could read every
--     user's duplicated purchases (empty today, which is why nothing leaked). It now
--     runs with the caller's rights, under credit_transactions' RLS, and only the
--     service role reads it.
--   * The updated_at triggers had a mutable search_path. They only call now(), so an
--     empty one is enough.
--   * wallet_funding_intents_unpaid, a definer view too, now runs with the caller's
--     rights, so wallet_funding_intents' RLS applies to it.
-- Performance
--   * RLS policies called auth.uid() once per row. Wrapped in a SELECT, Postgres
--     evaluates it once per statement (the auth_rls_initplan advisor). The policies
--     mean exactly what they meant: each is rewritten from its own current text.
--   * Indexes for the foreign keys that had none, so a delete that cascades from a
--     user or a kwami does not scan every child table.
-- =============================================================================

ALTER VIEW credit_transactions_suspected_duplicates SET (security_invoker = true);
REVOKE ALL ON credit_transactions_suspected_duplicates FROM anon, authenticated;

ALTER FUNCTION set_user_kwamis_updated_at() SET search_path = '';
ALTER FUNCTION set_kwami_communications_updated_at() SET search_path = '';
ALTER FUNCTION set_user_app_settings_updated_at() SET search_path = '';
ALTER FUNCTION set_wallet_updated_at() SET search_path = '';

-- Two more that migrations 004 and 016 create, and that the shared project does not
-- have (it was not built by scripts/migrate.py alone): fixed only where they exist.
DO $$
BEGIN
    IF to_regprocedure('public.set_updated_at_timestamp()') IS NOT NULL THEN
        ALTER FUNCTION set_updated_at_timestamp() SET search_path = '';
    END IF;
    -- Users read their own unpaid intents through it; with the caller's rights
    -- wallet_funding_intents' RLS decides which.
    IF to_regclass('public.wallet_funding_intents_unpaid') IS NOT NULL THEN
        ALTER VIEW wallet_funding_intents_unpaid SET (security_invoker = true);
    END IF;
END $$;

-- Policies: auth.uid() once per statement.
ALTER POLICY "Users can view own brain agents" ON brain_agents
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own brain cloud settings" ON brain_cloud_settings
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own brain jobs" ON brain_jobs
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own brain runners" ON brain_runners
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own brain sites" ON brain_sites
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own brain sync" ON brain_sync
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can delete own browser contexts" ON browser_contexts
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own browser contexts" ON browser_contexts
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own transactions" ON credit_transactions
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own usage" ON credit_usage_logs
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own assistant notifications" ON kwami_assistant_notifications
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own assistant settings" ON kwami_assistant_settings
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own assistant tasks" ON kwami_assistant_tasks
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own calendar events" ON kwami_calendar_events
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own kwami call events" ON kwami_call_events
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own kwami channels" ON kwami_channels
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own kwami contacts" ON kwami_contacts
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own kwami conversations" ON kwami_conversations
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own email accounts" ON kwami_email_accounts
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own email messages" ON kwami_email_messages
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own kwami message events" ON kwami_message_events
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own outbound audit" ON kwami_outbound_audit
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can manage own wallet key refs" ON kwami_wallet_key_refs
    USING (((select auth.uid()) = user_id))
    WITH CHECK (((select auth.uid()) = user_id));

ALTER POLICY "Users can manage own kwami wallets" ON kwami_wallets
    USING (((select auth.uid()) = user_id))
    WITH CHECK (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own sessions" ON livekit_sessions
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own usage reports" ON usage_reports
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can insert own app settings" ON user_app_settings
    WITH CHECK (((select auth.uid()) = user_id));

ALTER POLICY "Users can read own app settings" ON user_app_settings
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can update own app settings" ON user_app_settings
    USING (((select auth.uid()) = user_id))
    WITH CHECK (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own credits" ON user_credits
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can delete own kwamis" ON user_kwamis
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can insert own kwamis" ON user_kwamis
    WITH CHECK (((select auth.uid()) = user_id));

ALTER POLICY "Users can update own kwamis" ON user_kwamis
    USING (((select auth.uid()) = user_id))
    WITH CHECK (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own kwamis" ON user_kwamis
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can manage own balance cache" ON wallet_balances_cache
    USING (((select auth.uid()) = user_id))
    WITH CHECK (((select auth.uid()) = user_id));

ALTER POLICY "Users can view own funding events" ON wallet_funding_events
    USING (((select auth.uid()) = user_id));

ALTER POLICY "Users can manage own funding intents" ON wallet_funding_intents
    USING (((select auth.uid()) = user_id))
    WITH CHECK (((select auth.uid()) = user_id));

ALTER POLICY "Users can add custom allowlist tokens" ON wallet_token_allowlist
    WITH CHECK (((select auth.uid()) = created_by_user_id));

ALTER POLICY "Users can delete custom allowlist tokens" ON wallet_token_allowlist
    USING (((select auth.uid()) = created_by_user_id));

ALTER POLICY "Users can update custom allowlist tokens" ON wallet_token_allowlist
    USING (((select auth.uid()) = created_by_user_id))
    WITH CHECK (((select auth.uid()) = created_by_user_id));

ALTER POLICY "Users can manage own wallet transactions" ON wallet_transactions
    USING (((select auth.uid()) = user_id))
    WITH CHECK (((select auth.uid()) = user_id));


-- Migration 030 (jarvis_site_access) came to dev beside this one. Where it is there,
-- its policy gets the same treatment.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'public'
           AND tablename = 'jarvis_site_rules'
           AND policyname = 'Users can view own site rules'
    ) THEN
        ALTER POLICY "Users can view own site rules" ON jarvis_site_rules
            USING (((select auth.uid()) = user_id));
    END IF;
END $$;

-- Foreign keys with no index.
CREATE INDEX IF NOT EXISTS idx_brain_jobs_agent_id ON brain_jobs(agent_id);
CREATE INDEX IF NOT EXISTS idx_brain_jobs_kwami_id ON brain_jobs(kwami_id);
CREATE INDEX IF NOT EXISTS idx_brain_jobs_schedule_task_id ON brain_jobs(schedule_task_id);
CREATE INDEX IF NOT EXISTS idx_brain_sites_kwami_id ON brain_sites(kwami_id);
CREATE INDEX IF NOT EXISTS idx_brain_sync_job_id ON brain_sync(job_id);
CREATE INDEX IF NOT EXISTS idx_brain_sync_kwami_id ON brain_sync(kwami_id);
CREATE INDEX IF NOT EXISTS idx_credit_usage_logs_report_id ON credit_usage_logs(report_id);
CREATE INDEX IF NOT EXISTS idx_kwami_assistant_notifications_kwami_id ON kwami_assistant_notifications(kwami_id);
CREATE INDEX IF NOT EXISTS idx_kwami_assistant_notifications_task_id ON kwami_assistant_notifications(task_id);
CREATE INDEX IF NOT EXISTS idx_kwami_assistant_settings_user_id ON kwami_assistant_settings(user_id);
CREATE INDEX IF NOT EXISTS idx_kwami_assistant_tasks_agent_id ON kwami_assistant_tasks(agent_id);
CREATE INDEX IF NOT EXISTS idx_kwami_calendar_events_kwami_id ON kwami_calendar_events(kwami_id);
CREATE INDEX IF NOT EXISTS idx_kwami_call_events_channel_id ON kwami_call_events(channel_id);
CREATE INDEX IF NOT EXISTS idx_kwami_call_events_conversation_id ON kwami_call_events(conversation_id);
CREATE INDEX IF NOT EXISTS idx_kwami_call_events_kwami_id ON kwami_call_events(kwami_id);
CREATE INDEX IF NOT EXISTS idx_kwami_channels_kwami_id ON kwami_channels(kwami_id);
CREATE INDEX IF NOT EXISTS idx_kwami_contacts_kwami_id ON kwami_contacts(kwami_id);
CREATE INDEX IF NOT EXISTS idx_kwami_conversations_channel_id ON kwami_conversations(channel_id);
CREATE INDEX IF NOT EXISTS idx_kwami_conversations_contact_id ON kwami_conversations(contact_id);
CREATE INDEX IF NOT EXISTS idx_kwami_conversations_kwami_id ON kwami_conversations(kwami_id);
CREATE INDEX IF NOT EXISTS idx_kwami_email_accounts_kwami_id ON kwami_email_accounts(kwami_id);
CREATE INDEX IF NOT EXISTS idx_kwami_email_messages_account_id ON kwami_email_messages(account_id);
CREATE INDEX IF NOT EXISTS idx_kwami_email_messages_kwami_id ON kwami_email_messages(kwami_id);
CREATE INDEX IF NOT EXISTS idx_kwami_message_events_channel_id ON kwami_message_events(channel_id);
CREATE INDEX IF NOT EXISTS idx_kwami_message_events_contact_id ON kwami_message_events(contact_id);
CREATE INDEX IF NOT EXISTS idx_kwami_message_events_conversation_id ON kwami_message_events(conversation_id);
CREATE INDEX IF NOT EXISTS idx_kwami_message_events_kwami_id ON kwami_message_events(kwami_id);
CREATE INDEX IF NOT EXISTS idx_kwami_outbound_audit_kwami_id ON kwami_outbound_audit(kwami_id);
CREATE INDEX IF NOT EXISTS idx_kwami_outbound_audit_user_id ON kwami_outbound_audit(user_id);
CREATE INDEX IF NOT EXISTS idx_kwami_wallet_key_refs_kwami_id ON kwami_wallet_key_refs(kwami_id);
CREATE INDEX IF NOT EXISTS idx_wallet_balances_cache_kwami_id ON wallet_balances_cache(kwami_id);
CREATE INDEX IF NOT EXISTS idx_wallet_funding_events_intent_id ON wallet_funding_events(intent_id);
CREATE INDEX IF NOT EXISTS idx_wallet_funding_events_kwami_id ON wallet_funding_events(kwami_id);
CREATE INDEX IF NOT EXISTS idx_wallet_funding_events_wallet_id ON wallet_funding_events(wallet_id);
CREATE INDEX IF NOT EXISTS idx_wallet_funding_intents_kwami_id ON wallet_funding_intents(kwami_id);
CREATE INDEX IF NOT EXISTS idx_wallet_funding_intents_wallet_id ON wallet_funding_intents(wallet_id);
CREATE INDEX IF NOT EXISTS idx_wallet_token_allowlist_created_by_user_id ON wallet_token_allowlist(created_by_user_id);
CREATE INDEX IF NOT EXISTS idx_wallet_transactions_kwami_id ON wallet_transactions(kwami_id);
CREATE INDEX IF NOT EXISTS idx_wallet_transactions_related_intent_id ON wallet_transactions(related_intent_id);
