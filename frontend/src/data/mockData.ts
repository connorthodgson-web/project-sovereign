import {
  Bell,
  Bot,
  Brain,
  CalendarDays,
  CheckCircle2,
  Code2,
  Compass,
  Database,
  Eye,
  Gauge,
  Globe2,
  Inbox,
  LockKeyhole,
  Mail,
  MessageSquare,
  MonitorPlay,
  Search,
  Settings2,
  ShieldCheck,
  Slack,
  Sparkles,
  Timer,
  Workflow,
} from "lucide-react";
import type {
  AgentWorkstream,
  BrowserStatusResponse,
  CalendarEvent,
  ChatMessage,
  Integration,
  IntegrationStatusResponse,
  MemoryItem,
  MetricCard,
  NavItem,
  Reminder,
  SettingRow,
  TaskItem,
} from "../types";

export const navItems: NavItem[] = [
  { id: "chat", label: "CEO Chat", icon: MessageSquare },
  { id: "agents", label: "Agents", icon: Workflow },
  { id: "browser", label: "Browser", icon: MonitorPlay },
  { id: "life", label: "Life Ops", icon: CalendarDays },
  { id: "memory", label: "Memory", icon: Brain },
  { id: "settings", label: "Settings", icon: Settings2 },
];

export const initialMessages: ChatMessage[] = [
  {
    id: "m1",
    role: "assistant",
    content:
      "I am Sovereign: one CEO-style operator with specialist lanes behind me. Give me a goal, an assistant action, or a build task and I will plan, delegate, verify, and report back.",
    time: "09:04",
    mode: "CEO Operator",
  },
  {
    id: "m2",
    role: "assistant",
    content:
      "This console uses live read-only backend summaries when the API is online. Mock fallback panels are labeled so they never masquerade as real agent activity.",
    time: "09:05",
    mode: "Console v1",
  },
];

export const mockAgents: AgentWorkstream[] = [
  {
    id: "supervisor",
    name: "CEO/Supervisor",
    status: "idle",
    summary: "Owns intake, planning, delegation, review, and final responses.",
    last_action: "Ready for a goal.",
    evidence_count: 0,
    source: "mock_fallback",
  },
  {
    id: "research_agent",
    name: "Research Agent",
    status: "idle",
    summary: "Source-backed search and synthesis.",
    last_action: "No live run loaded.",
    evidence_count: 0,
    source: "mock_fallback",
  },
  {
    id: "browser_agent",
    name: "Browser Agent",
    status: "idle",
    summary: "Browser execution and evidence review.",
    last_action: "Waiting for browser task evidence.",
    evidence_count: 0,
    source: "mock_fallback",
  },
  {
    id: "browser_use",
    name: "Browser Use provider",
    status: "planned",
    summary: "Optional provider for richer browser workflows.",
    last_action: "Readiness placeholder only.",
    evidence_count: 0,
    source: "mock_fallback",
  },
  {
    id: "scheduling_agent",
    name: "Scheduling Agent",
    status: "idle",
    summary: "Reminders, calendar, and task scheduling.",
    last_action: "No live reminder run loaded.",
    evidence_count: 0,
    source: "mock_fallback",
  },
  {
    id: "communications_agent",
    name: "Communications Agent",
    status: "idle",
    summary: "Gmail, Slack outbound, and future notifications.",
    last_action: "No live communication run loaded.",
    evidence_count: 0,
    source: "mock_fallback",
  },
  {
    id: "coding_codex_agent",
    name: "Coding/Codex Agent",
    status: "idle",
    summary: "Workspace edits, runtime work, and Codex CLI lane.",
    last_action: "Codex readiness placeholder.",
    evidence_count: 0,
    source: "mock_fallback",
  },
  {
    id: "reviewer_verifier",
    name: "Reviewer/Verifier",
    status: "idle",
    summary: "Evidence review and final quality checks.",
    last_action: "Ready to review meaningful output.",
    evidence_count: 0,
    source: "mock_fallback",
  },
];

export const mockReminders: Reminder[] = [
  { id: "r1", summary: "Review active Sovereign task queue", deliver_at: "Today 4:30 PM", recurrence: "Daily", status: "mock" },
  { id: "r2", summary: "Prepare schoolwork check-in report", deliver_at: "Tomorrow 8:00 AM", recurrence: "Weekdays", status: "mock" },
];

export const mockCalendarEvents: CalendarEvent[] = [
  { id: "c1", title: "Operator loop review", time: "Today 1:00 PM", source: "Mock fallback", priority: "high" },
  { id: "c2", title: "Memory platform design pass", time: "Today 3:30 PM", source: "Mock fallback", priority: "medium" },
];

export const mockTasks: TaskItem[] = [
  { id: "t1", title: "Wire console chat to /chat", owner: "CEO Operator", status: "running", evidence_count: 0 },
  { id: "t2", title: "Keep dashboard mock-safe without credentials", owner: "Reviewer", status: "completed", evidence_count: 0 },
];

export const mockMemoryItems: MemoryItem[] = [
  {
    id: "mem1",
    title: "Product identity",
    body: "Sovereign should feel like one main CEO-style AI backed by specialist agents.",
    layer: "project",
  },
  {
    id: "mem2",
    title: "Autonomy preference",
    body: "Default to low-friction execution and ask follow-up questions only when truly needed.",
    layer: "preference",
  },
  {
    id: "mem3",
    title: "Current roadmap",
    body: "Slack-first loop, persistence, browser evidence, life ops, then dashboard visibility.",
    layer: "recent",
  },
  {
    id: "mem4",
    title: "Secrets boundary",
    body: "Credentials belong in secure storage and never in ordinary memory or frontend state.",
    layer: "safety",
  },
];

export const mockIntegrations: Integration[] = [
  { id: "openrouter", name: "OpenRouter", status: "configured", description: "Model routing/provider placeholder." },
  { id: "search", name: "Gemini Search", status: "configured", description: "Search readiness depends on OpenRouter credentials." },
  { id: "browser", name: "Browser", status: "configured", description: "Playwright and Browser Use readiness surface." },
  { id: "slack", name: "Slack", status: "configured", description: "First operator interface." },
  { id: "gmail", name: "Gmail", status: "planned", description: "Communications provider readiness." },
  { id: "calendar", name: "Calendar", status: "planned", description: "Life ops calendar readiness." },
  { id: "codex", name: "Codex", status: "configured", description: "Coding worker readiness placeholder." },
];

export const mockBrowser: BrowserStatusResponse = {
  source: "mock_fallback",
  mock: true,
  updated_at: new Date().toISOString(),
  status: "idle",
  human_action_required: false,
  blocker: null,
  evidence: {
    task_id: "mock-browser",
    result_status: "idle",
    requested_url: null,
    final_url: null,
    title: "No live browser evidence yet",
    headings: ["Ready for URL inspection", "Streaming placeholder only"],
    summary: "Browser execution can report safe evidence here once a real browser task runs.",
    text_preview: "Live browser streaming is not implemented in v1.",
    backend: "playwright/browser_use",
    headless: true,
    local_visible: false,
    screenshot: null,
    artifacts: [],
    blockers: [],
  },
  recent_artifacts: [],
  live_stream: {
    available: false,
    label: "Live browser streaming is not implemented yet.",
    future_ready: true,
  },
};

export const metricCards: MetricCard[] = [
  { label: "Operator mode", value: "CEO", detail: "one chat, many lanes", icon: Compass },
  { label: "Agent lanes", value: "8", detail: "standing console cards", icon: Bot },
  { label: "Evidence gate", value: "On", detail: "browser and review aware", icon: Eye },
  { label: "Autonomy", value: "High", detail: "blocked states escalate", icon: Gauge },
];

export const settingsRows: SettingRow[] = [
  { label: "Primary model provider", value: "OpenRouter placeholder", icon: Sparkles },
  { label: "Search provider", value: "Gemini via OpenRouter readiness", icon: Search },
  { label: "Browser mode", value: "Headless/visible shown from backend", icon: MonitorPlay },
  { label: "Browser Use", value: "Enabled/disabled readiness only", icon: CheckCircle2 },
  { label: "Codex worker", value: "Readiness placeholder, no frontend secrets", icon: Code2 },
  { label: "Slack", value: "First operator surface", icon: Slack },
  { label: "Gmail", value: "No secret editing in console", icon: Mail },
  { label: "Calendar and Tasks", value: "Readiness summaries only", icon: CalendarDays },
  { label: "Reminders", value: "Scheduler health from backend", icon: Timer },
  { label: "Memory provider", value: "Safe summaries, no raw secrets", icon: Database },
  { label: "Notifications", value: "Slack first, SMS planned", icon: Inbox },
  { label: "Safety", value: "High-risk sends stay gated", icon: ShieldCheck },
  { label: "Secrets", value: "Read-only status, no frontend storage", icon: LockKeyhole },
  { label: "Alerts", value: "Blocked states appear in cards", icon: Bell },
];

export function integrationResponseFallback(): IntegrationStatusResponse {
  return {
    source: "mock_fallback",
    mock: true,
    updated_at: new Date().toISOString(),
    model_provider: {
      primary: "OpenRouter placeholder",
      routing_enabled: true,
      configured: false,
      placeholder_only: true,
    },
    search: {
      provider: "gemini",
      configured: false,
      enabled: false,
      status: "mock",
    },
    browser: {
      mode: "auto",
      headless: true,
      visible: false,
      browser_use_enabled: false,
      streaming_live: false,
    },
    integrations: mockIntegrations,
  };
}
