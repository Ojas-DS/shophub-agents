-- Runs once on first start. The orchestrator uses the default "orchestrator"
-- database; the orders agent gets its own, so the two services own separate
-- data (checkpoints + A2A tasks) and never race on the same migrations.
CREATE DATABASE orders_agent OWNER shophub;
