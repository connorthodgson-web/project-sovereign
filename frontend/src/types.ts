import type { LucideIcon } from "lucide-react";

export type ConnectionState = "checking" | "online" | "offline";
export type AgentStatus = "idle" | "running" | "blocked" | "completed" | "planned";
export type IntegrationStatus = "live" | "connected" | "configured" | "configured_but_disabled" | "missing" | "planned" | "scaffolded" | "unavailable" | "not_live" | "unknown";
export type TaskStatus = "pending" | "queued" | "planning" | "planned" | "routing" | "running" | "reviewing" | "completed" | "blocked" | "failed";

export interface NavItem {
  id: string;
  label: string;
  icon: LucideIcon;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  time: string;
  mode?: string;
}

export interface AgentWorkstream {
  id: string;
  name: string;
  role?: string;
  summary?: string;
  status: AgentStatus;
  currentFocus?: string;
  last_action?: string;
  progress?: number;
  evidence?: string;
  evidence_count?: number;
  blocker?: string | null;
  source?: string;
}

export interface Reminder {
  id: string;
  title?: string;
  summary?: string;
  time?: string;
  deliver_at?: string;
  cadence?: string;
  recurrence?: string | null;
  status: string;
  schedule_kind?: string;
  delivery_channel?: string;
}

export interface CalendarEvent {
  id: string;
  title: string;
  time?: string;
  start?: string;
  end?: string;
  source?: string;
  priority?: "high" | "medium" | "low";
  status?: string;
  location?: string | null;
}

export interface TaskItem {
  id: string;
  title: string;
  owner?: string;
  status: TaskStatus | string;
  summary?: string | null;
  goal?: string;
  evidence_count?: number;
}

export interface MemoryItem {
  id: string;
  title: string;
  body: string;
  layer: "project" | "preference" | "recent" | "safety" | "user" | "operational";
}

export interface Integration {
  id: string;
  name: string;
  status: IntegrationStatus | string;
  description: string;
  configured?: boolean;
  enabled?: boolean;
  missing_fields?: string[];
  notes?: string[];
}

export interface MetricCard {
  label: string;
  value: string;
  detail: string;
  icon: LucideIcon;
}

export interface SettingRow {
  label: string;
  value: string;
  icon: LucideIcon;
  status?: string;
}

export interface BackendChatResponse {
  task_id: string;
  status: string;
  planner_mode: string;
  request_mode: string;
  escalation_level: string;
  response: string;
  subtasks?: Array<{ id: string; title: string; status: string; assigned_agent?: string | null }>;
}

export interface AgentsStatusResponse {
  source: string;
  mock: boolean;
  updated_at: string;
  agents: AgentWorkstream[];
}

export interface RunsStatusResponse {
  source: string;
  mock: boolean;
  updated_at: string;
  agents: AgentWorkstream[];
  runs: TaskItem[];
}

export interface IntegrationStatusResponse {
  source: string;
  mock: boolean;
  updated_at: string;
  model_provider: {
    primary: string;
    routing_enabled: boolean;
    configured: boolean;
    placeholder_only: boolean;
  };
  search: {
    provider: string;
    configured: boolean;
    enabled: boolean;
    status: string;
  };
  browser: {
    mode: string;
    headless: boolean;
    visible: boolean;
    browser_use_enabled: boolean;
    streaming_live: boolean;
  };
  integrations: Integration[];
}

export interface MemorySummaryResponse {
  source: string;
  mock: boolean;
  updated_at: string;
  provider: string;
  counts: Record<string, number>;
  facts: Array<{
    layer: MemoryItem["layer"];
    category: string;
    key: string;
    value: string;
    confidence?: number;
    updated_at?: string;
  }>;
  recent_actions: Array<{ summary: string; status: string; kind: string; created_at: string }>;
  open_loops: Array<{ summary: string; status: string; updated_at: string }>;
  secrets_exposed: boolean;
}

export interface LifeRemindersResponse {
  source: string;
  mock: boolean;
  updated_at: string;
  scheduler: {
    live: boolean;
    started: boolean;
    status: string;
    blockers: string[];
  };
  reminders: Reminder[];
}

export interface LifeCalendarResponse {
  source: string;
  mock: boolean;
  updated_at: string;
  status: string;
  summary: string;
  events: CalendarEvent[];
  blockers: string[];
}

export interface LifeTasksResponse {
  source: string;
  mock: boolean;
  updated_at: string;
  status: string;
  summary: string;
  operator_tasks: TaskItem[];
  external_tasks: TaskItem[];
  blockers: string[];
}

export interface BrowserArtifact {
  path: string;
  name: string;
  exists: boolean;
  preview_available: boolean;
  task_id?: string;
  result_status?: string;
}

export interface BrowserStatusResponse {
  source: string;
  mock: boolean;
  updated_at: string;
  status: "idle" | "running" | "blocked" | "completed" | string;
  blocker?: string | null;
  human_action_required: boolean;
  evidence?: {
    task_id: string;
    result_status: string;
    requested_url?: string | null;
    final_url?: string | null;
    title?: string | null;
    headings: string[];
    summary?: string;
    text_preview?: string | null;
    backend?: string;
    headless?: boolean;
    local_visible?: boolean;
    screenshot?: BrowserArtifact | null;
    artifacts: BrowserArtifact[];
    blockers: string[];
  } | null;
  recent_artifacts: BrowserArtifact[];
  live_stream: {
    available: boolean;
    label: string;
    future_ready: boolean;
  };
}
