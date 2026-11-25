-- The first sign-in guide's state, per person and workspace (dismissed, ...).
ALTER TABLE memberships ADD COLUMN IF NOT EXISTS guide TEXT NOT NULL DEFAULT '{}';
