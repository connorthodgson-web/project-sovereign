import type {
  AgentsStatusResponse,
  BackendChatResponse,
  BrowserStatusResponse,
  IntegrationStatusResponse,
  LifeCalendarResponse,
  LifeRemindersResponse,
  LifeTasksResponse,
  MemorySummaryResponse,
  RunsStatusResponse,
} from "../types";

const API_BASE_URL = import.meta.env.VITE_SOVEREIGN_API_URL ?? (import.meta.env.DEV ? "/api" : "http://127.0.0.1:8000");

async function fetchWithTimeout(path: string, options: RequestInit = {}, timeoutMs = 3500) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      ...options,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(options.headers ?? {}),
      },
    });

    if (!response.ok) {
      throw new Error(`Request failed with ${response.status}`);
    }

    return response;
  } finally {
    window.clearTimeout(timeout);
  }
}

async function getJson<T>(path: string, timeoutMs?: number): Promise<T> {
  const response = await fetchWithTimeout(path, {}, timeoutMs);
  return (await response.json()) as T;
}

export async function checkHealth(): Promise<boolean> {
  try {
    const response = await fetchWithTimeout("/health");
    const data = (await response.json()) as { status?: string };
    return data.status === "ok";
  } catch {
    return false;
  }
}

export async function sendChatMessage(message: string): Promise<BackendChatResponse> {
  const response = await fetchWithTimeout(
    "/chat",
    {
      method: "POST",
      body: JSON.stringify({ message, transport: "dashboard" }),
    },
    15000,
  );

  return (await response.json()) as BackendChatResponse;
}

export function getAgentsStatus() {
  return getJson<AgentsStatusResponse>("/agents/status");
}

export function getRunsStatus() {
  return getJson<RunsStatusResponse>("/runs/status");
}

export function getIntegrationsStatus() {
  return getJson<IntegrationStatusResponse>("/integrations/status");
}

export function getMemorySummary() {
  return getJson<MemorySummaryResponse>("/memory/summary");
}

export function getLifeReminders() {
  return getJson<LifeRemindersResponse>("/life/reminders");
}

export function getLifeCalendar() {
  return getJson<LifeCalendarResponse>("/life/calendar");
}

export function getLifeTasks() {
  return getJson<LifeTasksResponse>("/life/tasks");
}

export function getBrowserStatus() {
  return getJson<BrowserStatusResponse>("/browser/status");
}

export { API_BASE_URL };
