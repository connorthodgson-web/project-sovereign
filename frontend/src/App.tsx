import { FormEvent, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Bot,
  CheckCircle2,
  Circle,
  Clock3,
  Copy,
  Database,
  Eye,
  FileImage,
  Globe2,
  Layers3,
  Loader2,
  LockKeyhole,
  Maximize2,
  MessageSquare,
  MonitorPlay,
  Radio,
  RefreshCw,
  Search,
  Send,
  Shield,
  SlidersHorizontal,
  Sparkles,
  Wifi,
  WifiOff,
} from "lucide-react";
import {
  initialMessages,
  integrationResponseFallback,
  metricCards,
  mockAgents,
  mockBrowser,
  mockCalendarEvents,
  mockMemoryItems,
  mockReminders,
  mockTasks,
  navItems,
  settingsRows,
} from "./data/mockData";
import {
  API_BASE_URL,
  checkHealth,
  getAgentsStatus,
  getBrowserStatus,
  getIntegrationsStatus,
  getLifeCalendar,
  getLifeReminders,
  getLifeTasks,
  getMemorySummary,
  getRunsStatus,
  sendChatMessage,
} from "./lib/apiClient";
import type {
  AgentStatus,
  AgentWorkstream,
  BrowserStatusResponse,
  CalendarEvent,
  ChatMessage,
  ConnectionState,
  Integration,
  IntegrationStatusResponse,
  LifeCalendarResponse,
  LifeRemindersResponse,
  LifeTasksResponse,
  MemoryItem,
  MemorySummaryResponse,
  Reminder,
  RunsStatusResponse,
  TaskItem,
} from "./types";

const statusLabels: Record<ConnectionState, string> = {
  checking: "Checking",
  online: "Live API",
  offline: "Mock fallback",
};

const agentStatusLabels: Record<AgentStatus, string> = {
  idle: "Idle",
  running: "Running",
  blocked: "Blocked",
  completed: "Complete",
  planned: "Planned",
};

type Tone = "neutral" | "good" | "warn" | "danger" | "live" | "mock";

function nowTime() {
  return new Intl.DateTimeFormat([], { hour: "2-digit", minute: "2-digit" }).format(new Date());
}

function formatDateTime(value?: string | null) {
  if (!value) return "No time set";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return new Intl.DateTimeFormat([], {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(parsed);
}

function titleCase(value: string) {
  return value
    .replace(/^integration:/, "")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function statusTone(status?: string | null): Tone {
  const normalized = String(status ?? "").toLowerCase();
  if (["completed", "complete", "live", "connected", "ok"].includes(normalized)) return "good";
  if (["running", "configured", "checking"].includes(normalized)) return "live";
  if (["blocked", "failed", "missing", "unavailable", "not_live"].includes(normalized)) return "danger";
  if (["planned", "scaffolded", "configured_but_disabled", "reviewing"].includes(normalized)) return "warn";
  if (["mock", "mock_fallback"].includes(normalized)) return "mock";
  return "neutral";
}

function StatusPill({ label, tone }: { label: string; tone: Tone }) {
  return <span className={`status-pill ${tone}`}>{label}</span>;
}

function SourceBadge({ mock, source }: { mock?: boolean; source?: string }) {
  return (
    <span className={`source-badge ${mock ? "mock" : "live"}`}>
      {mock ? "Mock" : "Live"}
      {source ? <span>{source.replace(/_/g, " ")}</span> : null}
    </span>
  );
}

function EmptyState({ icon: Icon, text }: { icon: typeof Circle; text: string }) {
  return (
    <div className="empty-state">
      <Icon size={18} />
      <span>{text}</span>
    </div>
  );
}

function App() {
  const [activeSection, setActiveSection] = useState("chat");
  const [connection, setConnection] = useState<ConnectionState>("checking");
  const [messages, setMessages] = useState<ChatMessage[]>(initialMessages);
  const [draftMessage, setDraftMessage] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [accent, setAccent] = useState("aqua");
  const [agentsData, setAgentsData] = useState<AgentWorkstream[]>(mockAgents);
  const [runsData, setRunsData] = useState<RunsStatusResponse | null>(null);
  const [integrationsData, setIntegrationsData] = useState<IntegrationStatusResponse>(integrationResponseFallback);
  const [memoryData, setMemoryData] = useState<MemorySummaryResponse | null>(null);
  const [remindersData, setRemindersData] = useState<LifeRemindersResponse | null>(null);
  const [calendarData, setCalendarData] = useState<LifeCalendarResponse | null>(null);
  const [tasksData, setTasksData] = useState<LifeTasksResponse | null>(null);
  const [browserData, setBrowserData] = useState<BrowserStatusResponse>(mockBrowser);
  const [refreshing, setRefreshing] = useState(false);

  const dashboardOnline = connection === "online";
  const reminders = dashboardOnline ? remindersData?.reminders ?? [] : mockReminders;
  const calendarEvents = dashboardOnline ? calendarData?.events ?? [] : mockCalendarEvents;
  const taskItems = dashboardOnline ? [...(tasksData?.operator_tasks ?? []), ...(tasksData?.external_tasks ?? [])] : mockTasks;
  const memoryItems = useMemo(() => mapMemoryItems(memoryData, dashboardOnline), [dashboardOnline, memoryData]);
  const integrations = integrationsData.integrations.length ? integrationsData.integrations : integrationResponseFallback().integrations;

  const onlineCount = useMemo(
    () =>
      integrations.filter((integration) =>
        ["live", "connected", "configured"].includes(String(integration.status).toLowerCase()),
      ).length,
    [integrations],
  );

  const dynamicMetrics = useMemo(() => {
    const activeRuns = runsData?.runs?.filter((run) => !["completed", "failed"].includes(String(run.status))).length ?? 0;
    const totalEvidence = agentsData.reduce((sum, agent) => sum + (agent.evidence_count ?? 0), 0);
    return metricCards.map((metric) => {
      if (metric.label === "Agent lanes") return { ...metric, value: String(agentsData.length), detail: `${onlineCount} integrations ready` };
      if (metric.label === "Evidence gate") return { ...metric, value: totalEvidence ? String(totalEvidence) : "On", detail: "safe summaries only" };
      if (metric.label === "Operator mode") return { ...metric, detail: `${activeRuns} active/recent goals` };
      return metric;
    });
  }, [agentsData, onlineCount, runsData]);

  async function refreshDashboard() {
    setRefreshing(true);
    const online = await checkHealth();
    setConnection(online ? "online" : "offline");

    if (!online) {
      setAgentsData(mockAgents);
      setIntegrationsData(integrationResponseFallback());
      setBrowserData(mockBrowser);
      setRefreshing(false);
      return;
    }

    const [agents, runs, integrationsResult, memory, remindersResult, calendar, tasks, browser] = await Promise.allSettled([
      getAgentsStatus(),
      getRunsStatus(),
      getIntegrationsStatus(),
      getMemorySummary(),
      getLifeReminders(),
      getLifeCalendar(),
      getLifeTasks(),
      getBrowserStatus(),
    ]);

    if (agents.status === "fulfilled") setAgentsData(agents.value.agents);
    if (runs.status === "fulfilled") setRunsData(runs.value);
    if (integrationsResult.status === "fulfilled") setIntegrationsData(integrationsResult.value);
    if (memory.status === "fulfilled") setMemoryData(memory.value);
    if (remindersResult.status === "fulfilled") setRemindersData(remindersResult.value);
    if (calendar.status === "fulfilled") setCalendarData(calendar.value);
    if (tasks.status === "fulfilled") setTasksData(tasks.value);
    if (browser.status === "fulfilled") setBrowserData(browser.value);
    setRefreshing(false);
  }

  useEffect(() => {
    void refreshDashboard();
  }, []);

  function jumpTo(section: string) {
    setActiveSection(section);
    document.getElementById(section)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function handleSend(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = draftMessage.trim();
    if (!trimmed || isSending) return;

    const userMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: trimmed,
      time: nowTime(),
    };

    setMessages((current) => [...current, userMessage]);
    setDraftMessage("");
    setIsSending(true);

    try {
      const response = await sendChatMessage(trimmed);
      setConnection("online");
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content: response.response,
          time: nowTime(),
          mode: `${response.request_mode} / ${response.status}`,
        },
      ]);
      void refreshDashboard();
    } catch {
      setConnection("offline");
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content:
            "The backend is not reachable from the console right now. I can keep the interface warm in mock fallback mode, and /chat will reconnect as soon as the API is running.",
          time: nowTime(),
          mode: "Offline fallback",
        },
      ]);
    } finally {
      setIsSending(false);
    }
  }

  return (
    <main className={`app-shell accent-${accent}`}>
      <aside className="sidebar">
        <div className="brand-lockup">
          <div className="brand-mark">
            <Sparkles size={24} />
          </div>
          <div>
            <p className="eyebrow">Project Sovereign</p>
            <h1>AI Hub</h1>
          </div>
        </div>

        <nav className="nav-list" aria-label="Console sections">
          {navItems.map((item) => {
            const Icon = item.icon;
            return (
              <button
                className={`nav-item ${activeSection === item.id ? "active" : ""}`}
                key={item.id}
                onClick={() => jumpTo(item.id)}
                type="button"
              >
                <Icon size={18} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>

        <div className="sidebar-card">
          <div className="connection-row">
            {connection === "online" ? <Wifi size={18} /> : <WifiOff size={18} />}
            <span>{statusLabels[connection]}</span>
          </div>
          <code>{API_BASE_URL}</code>
          <button className="ghost-button" onClick={refreshDashboard} type="button" disabled={refreshing}>
            {refreshing ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
            <span>Refresh</span>
          </button>
        </div>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <div className="topline">
              <Radio size={18} />
              <span>One operator, visible workstreams</span>
            </div>
            <h2>Sovereign Operator Console</h2>
          </div>
          <div className="topbar-actions">
            <StatusPill label={statusLabels[connection]} tone={connection === "online" ? "good" : connection === "offline" ? "mock" : "neutral"} />
            <button className="icon-button" type="button" aria-label="Refresh dashboard" onClick={refreshDashboard}>
              {refreshing ? <Loader2 className="spin" size={18} /> : <RefreshCw size={18} />}
            </button>
          </div>
        </header>

        <section className="metric-grid" aria-label="Operator metrics">
          {dynamicMetrics.map((metric) => {
            const Icon = metric.icon;
            return (
              <article className="metric-card" key={metric.label}>
                <div className="metric-icon">
                  <Icon size={20} />
                </div>
                <div>
                  <p>{metric.label}</p>
                  <strong>{metric.value}</strong>
                  <span>{metric.detail}</span>
                </div>
              </article>
            );
          })}
        </section>

        <div className="console-grid">
          <section className="panel chat-panel" id="chat">
            <PanelHeading
              eyebrow="CEO Chat"
              title="Main Operator"
              source={<SourceBadge mock={connection !== "online"} source={connection === "online" ? "chat endpoint" : "mock fallback"} />}
            />

            <div className="message-list" aria-live="polite">
              {messages.map((message) => (
                <article className={`message ${message.role}`} key={message.id}>
                  <div className="message-meta">
                    <span>{message.role === "assistant" ? "Sovereign" : "You"}</span>
                    <span>{message.time}</span>
                  </div>
                  <p>{message.content}</p>
                  {message.mode ? <span className="mode-chip">{message.mode}</span> : null}
                </article>
              ))}
            </div>

            <form className="chat-input-row" onSubmit={handleSend}>
              <div className="chat-input-shell">
                <MessageSquare size={18} />
                <input
                  aria-label="Message Sovereign"
                  onChange={(event) => setDraftMessage(event.target.value)}
                  placeholder="Give Sovereign a goal..."
                  value={draftMessage}
                />
              </div>
              <button className="send-button" type="submit" disabled={!draftMessage.trim() || isSending}>
                {isSending ? <Loader2 className="spin" size={18} /> : <Send size={18} />}
                <span>Send</span>
              </button>
            </form>
          </section>

          <aside className="panel agent-rail" id="agents">
            <PanelHeading
              eyebrow="Workstreams"
              title="Agent Activity"
              source={<SourceBadge mock={!dashboardOnline} source={dashboardOnline ? "agents status" : "mock fallback"} />}
            />
            <div className="agent-list">
              {agentsData.map((agent) => (
                <article className="agent-card" key={agent.id}>
                  <div className="agent-card-header">
                    <div className="agent-avatar">
                      <Bot size={17} />
                    </div>
                    <div>
                      <h3>{agent.name}</h3>
                      <p>{agent.summary ?? agent.role}</p>
                    </div>
                    <StatusPill label={agentStatusLabels[agent.status] ?? titleCase(agent.status)} tone={statusTone(agent.status)} />
                  </div>
                  <p className="agent-action">{agent.last_action ?? agent.currentFocus}</p>
                  <div className="agent-footer">
                    <span>
                      <Shield size={14} />
                      {agent.evidence_count ?? 0} evidence
                    </span>
                    {agent.blocker ? (
                      <span className="agent-blocker">
                        <AlertTriangle size={14} />
                        {agent.blocker}
                      </span>
                    ) : null}
                  </div>
                </article>
              ))}
            </div>
          </aside>
        </div>

        <section className="panel browser-panel" id="browser">
          <PanelHeading
            eyebrow="Browser Workspace"
            title="Status and Evidence"
            source={<SourceBadge mock={browserData.mock} source={browserData.source} />}
          />
          <div className="browser-grid">
            <div className="browser-stage">
              <div className="browser-toolbar">
                <span />
                <span />
                <span />
                <strong>{browserData.evidence?.title ?? titleCase(browserData.status)}</strong>
                <button className="icon-button compact" type="button" aria-label="Future fullscreen browser stream">
                  <Maximize2 size={15} />
                </button>
              </div>
              <div className="browser-viewport">
                <MonitorPlay size={42} />
                <h3>{browserData.live_stream.available ? "Live browser" : "Live-ready placeholder"}</h3>
                <p>{browserData.live_stream.label}</p>
                <StatusPill label={titleCase(browserData.status)} tone={statusTone(browserData.status)} />
              </div>
            </div>

            <div className="browser-details">
              <div className="detail-grid">
                <Detail label="Requested URL" value={browserData.evidence?.requested_url ?? "None"} />
                <Detail label="Final URL" value={browserData.evidence?.final_url ?? "None"} />
                <Detail label="Backend" value={browserData.evidence?.backend ?? integrationsData.browser.mode} />
                <Detail label="Window" value={browserData.evidence?.local_visible ? "Visible" : integrationsData.browser.visible ? "Visible" : "Headless"} />
              </div>

              {browserData.blocker ? (
                <div className="blocker-banner">
                  <AlertTriangle size={18} />
                  <span>{browserData.blocker}</span>
                </div>
              ) : null}

              <div className="evidence-box">
                <div className="evidence-title">
                  <Eye size={17} />
                  <strong>Evidence Summary</strong>
                </div>
                <p>{browserData.evidence?.summary ?? "No browser evidence has been captured yet."}</p>
                <div className="heading-cloud">
                  {(browserData.evidence?.headings ?? []).slice(0, 5).map((heading) => (
                    <span key={heading}>{heading}</span>
                  ))}
                </div>
              </div>

              <div className="artifact-row">
                <FileImage size={18} />
                <div>
                  <strong>{browserData.evidence?.screenshot?.name ?? "No screenshot artifact"}</strong>
                  <span>{browserData.evidence?.screenshot?.path ?? "Path appears after safe workspace capture."}</span>
                </div>
              </div>
            </div>
          </div>
        </section>

        <section className="life-grid" id="life">
          <Panel className="life-panel" eyebrow="Life Ops" title="Reminders" mock={!dashboardOnline} source={remindersData?.source}>
            {reminders.length ? (
              <div className="compact-list">
                {reminders.map((reminder) => (
                  <article className="compact-item" key={reminder.id}>
                    <Clock3 size={16} />
                    <div>
                      <strong>{reminder.summary ?? reminder.title}</strong>
                      <span>
                        {formatDateTime(reminder.deliver_at ?? reminder.time)} · {reminder.recurrence ?? reminder.cadence ?? reminder.schedule_kind ?? "one-time"}
                      </span>
                    </div>
                    <StatusPill label={titleCase(reminder.status)} tone={statusTone(reminder.status)} />
                  </article>
                ))}
              </div>
            ) : (
              <EmptyState icon={Clock3} text="No safe reminders loaded." />
            )}
          </Panel>

          <Panel className="life-panel" eyebrow="Life Ops" title="Calendar" mock={!dashboardOnline} source={calendarData?.source}>
            {calendarEvents.length ? (
              <div className="compact-list">
                {calendarEvents.map((event) => (
                  <article className="compact-item" key={event.id}>
                    <Globe2 size={16} />
                    <div>
                      <strong>{event.title}</strong>
                      <span>{formatDateTime(event.start ?? event.time)} · {event.source ?? event.status ?? "calendar"}</span>
                    </div>
                    <StatusPill label={event.priority ? titleCase(event.priority) : titleCase(event.status ?? "ready")} tone={statusTone(event.status ?? event.priority)} />
                  </article>
                ))}
              </div>
            ) : (
              <EmptyState icon={Globe2} text={calendarData?.summary ?? "No safe calendar events loaded."} />
            )}
          </Panel>

          <Panel className="life-panel wide" eyebrow="Life Ops" title="Tasks" mock={!dashboardOnline} source={tasksData?.source}>
            {taskItems.length ? (
              <div className="task-strip">
                {taskItems.slice(0, 6).map((task) => (
                  <article className="task-card" key={task.id}>
                    <div>
                      <strong>{task.title}</strong>
                      <span>{task.summary ?? task.goal ?? task.owner ?? "Operator task"}</span>
                    </div>
                    <StatusPill label={titleCase(String(task.status))} tone={statusTone(String(task.status))} />
                  </article>
                ))}
              </div>
            ) : (
              <EmptyState icon={Layers3} text={tasksData?.summary ?? "No safe tasks loaded."} />
            )}
          </Panel>
        </section>

        <section className="panel memory-panel" id="memory">
          <PanelHeading
            eyebrow="Memory"
            title="Safe Context"
            source={<SourceBadge mock={!dashboardOnline || !memoryData} source={memoryData?.source ?? "mock fallback"} />}
          />
          <div className="memory-layout">
            <div className="memory-grid">
              {memoryItems.length ? (
                memoryItems.map((item) => (
                  <article className="memory-card" key={item.id}>
                    <span className={`memory-token ${item.layer}`}>{titleCase(item.layer)}</span>
                    <h3>{item.title}</h3>
                    <p>{item.body}</p>
                  </article>
                ))
              ) : (
                <EmptyState icon={Database} text="No safe memory facts are available yet." />
              )}
            </div>
            <div className="memory-stats">
              <div>
                <Database size={18} />
                <span>Provider</span>
                <strong>{memoryData?.provider ?? "mock"}</strong>
              </div>
              <div>
                <LockKeyhole size={18} />
                <span>Secrets exposed</span>
                <strong>{memoryData?.secrets_exposed ? "Review" : "No"}</strong>
              </div>
              <div>
                <Activity size={18} />
                <span>Open loops</span>
                <strong>{memoryData?.counts?.open_loops ?? 0}</strong>
              </div>
            </div>
          </div>
        </section>

        <section className="panel integrations-panel" id="settings">
          <PanelHeading
            eyebrow="Integrations"
            title="Settings and Readiness"
            source={<SourceBadge mock={integrationsData.mock} source={integrationsData.source} />}
          />
          <div className="settings-layout">
            <div className="provider-card">
              <div>
                <Sparkles size={22} />
                <span>Model provider</span>
              </div>
              <strong>{integrationsData.model_provider.primary}</strong>
              <p>{integrationsData.model_provider.routing_enabled ? "Routing metadata enabled" : "Routing metadata disabled"}</p>
            </div>
            <div className="provider-card">
              <div>
                <Search size={22} />
                <span>Search</span>
              </div>
              <strong>{titleCase(integrationsData.search.provider)}</strong>
              <p>{titleCase(integrationsData.search.status)}</p>
            </div>
            <div className="provider-card">
              <div>
                <MonitorPlay size={22} />
                <span>Browser</span>
              </div>
              <strong>{titleCase(integrationsData.browser.mode)}</strong>
              <p>{integrationsData.browser.visible ? "Visible local browser" : "Headless local browser"}</p>
            </div>
          </div>

          <div className="integration-grid">
            {integrations.slice(0, 12).map((integration) => (
              <article className="integration-card" key={integration.id}>
                <div className="integration-top">
                  <strong>{titleCase(integration.id || integration.name)}</strong>
                  <StatusPill label={titleCase(String(integration.status))} tone={statusTone(String(integration.status))} />
                </div>
                <p>{integration.notes?.[0] ?? integration.description ?? "Readiness summary loaded."}</p>
              </article>
            ))}
          </div>

          <div className="settings-grid">
            {settingsRows.map((row) => {
              const Icon = row.icon;
              return (
                <article className="setting-row" key={row.label}>
                  <div className="setting-icon">
                    <Icon size={18} />
                  </div>
                  <div>
                    <strong>{row.label}</strong>
                    <span>{row.value}</span>
                  </div>
                  <SlidersHorizontal size={16} />
                </article>
              );
            })}
          </div>
        </section>
      </section>
    </main>
  );
}

function PanelHeading({
  eyebrow,
  title,
  source,
}: {
  eyebrow: string;
  title: string;
  source: ReactNode;
}) {
  return (
    <div className="section-heading">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h2>{title}</h2>
      </div>
      {source}
    </div>
  );
}

function Panel({
  children,
  className,
  eyebrow,
  title,
  mock,
  source,
}: {
  children: ReactNode;
  className?: string;
  eyebrow: string;
  title: string;
  mock: boolean;
  source?: string;
}) {
  return (
    <section className={`panel ${className ?? ""}`}>
      <PanelHeading eyebrow={eyebrow} title={title} source={<SourceBadge mock={mock} source={source ?? (mock ? "mock fallback" : "live backend")} />} />
      {children}
    </section>
  );
}

function Detail({ label, value }: { label: string; value?: string | null }) {
  return (
    <div className="detail-card">
      <span>{label}</span>
      <strong>{value || "None"}</strong>
    </div>
  );
}

function mapMemoryItems(memoryData: MemorySummaryResponse | null, online: boolean): MemoryItem[] {
  if (!online || !memoryData) return mockMemoryItems;
  const facts = memoryData.facts.map((fact) => ({
    id: `${fact.layer}-${fact.key}`,
    title: titleCase(fact.key),
    body: fact.value,
    layer: fact.layer,
  }));
  const actions = memoryData.recent_actions.slice(0, 2).map((action, index) => ({
    id: `action-${index}`,
    title: titleCase(action.kind),
    body: action.summary,
    layer: "recent" as const,
  }));
  return [...facts, ...actions].slice(0, 8);
}

export default App;
