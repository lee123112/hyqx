PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS datasets (
  id TEXT NOT NULL,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  evaluation_types_json TEXT NOT NULL,
  modality TEXT,
  pipeline TEXT,
  task_id TEXT,
  batch_id TEXT,
  version TEXT,
  source TEXT,
  status TEXT,
  mode TEXT,
  samples_json TEXT NOT NULL,
  count INTEGER NOT NULL,
  sample_hash TEXT,
  standard TEXT,
  note TEXT,
  created_at TEXT,
  updated_at TEXT,
  PRIMARY KEY (id, kind)
);
CREATE INDEX IF NOT EXISTS idx_datasets_kind_type ON datasets(kind, type);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  step INTEGER NOT NULL DEFAULT 0,
  created_at TEXT,
  completed_at TEXT,
  error TEXT,
  dataset_id TEXT,
  parent_id TEXT,
  payload_json TEXT NOT NULL,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(type);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);

CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  sample_id TEXT NOT NULL,
  issue TEXT,
  source_step TEXT,
  suggestion TEXT,
  parameters_json TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  note TEXT,
  parameters_after_json TEXT,
  verification_id TEXT,
  performance_before_json TEXT,
  performance_after_json TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_feedback_task ON feedback(task_id);
CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback(status);

CREATE TABLE IF NOT EXISTS baselines (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  model_id TEXT,
  mode TEXT,
  name TEXT NOT NULL,
  description TEXT,
  version TEXT,
  status TEXT,
  validation_version TEXT,
  seed INTEGER,
  config_json TEXT,
  model_name TEXT,
  source TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_baselines_type ON baselines(type);

CREATE TABLE IF NOT EXISTS task_logs (
  task_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  time TEXT NOT NULL,
  tag TEXT,
  message TEXT NOT NULL,
  PRIMARY KEY (task_id, seq)
);

CREATE TABLE IF NOT EXISTS operator_runs (
  unique_id TEXT NOT NULL,
  operator_id TEXT NOT NULL,
  status TEXT NOT NULL,
  params_json TEXT,
  result_json TEXT,
  workspace TEXT,
  created_at TEXT,
  finished_at TEXT,
  PRIMARY KEY (unique_id, operator_id)
);

CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_type TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  kind TEXT,
  path TEXT NOT NULL,
  size INTEGER,
  sha256 TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_artifacts_owner ON artifacts(owner_type, owner_id);
