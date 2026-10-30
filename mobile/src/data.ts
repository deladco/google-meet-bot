import { supabase } from "./supabase";

export type Minutes = {
  summary?: string;
  decisions?: { decision?: string }[];
  action_items?: { task?: string; owner?: string; deadline?: string }[];
  discussion_topics?: { topic?: string; details?: string }[];
  unresolved_questions?: { question?: string }[];
};

export type Meeting = {
  meeting_id: string;
  recorded_at: string | null;
  status: string | null;
  notes: string | null;
  minutes: Minutes | null;
  transcript?: string;
};

export type TranscriptLine = { speaker?: string; text?: string; start?: number };

const LIST_COLUMNS = "meeting_id,recorded_at,status,notes,minutes";

export async function listMeetings(): Promise<Meeting[]> {
  const { data, error } = await supabase
    .from("synced_meetings")
    .select(LIST_COLUMNS)
    .order("recorded_at", { ascending: false })
    .limit(200);
  if (error) throw error;
  return (data ?? []).map((row) => ({ ...row, minutes: parseMinutes(row.minutes) }));
}

export async function getMeeting(meetingId: string): Promise<Meeting | null> {
  const { data, error } = await supabase
    .from("synced_meetings")
    .select(`${LIST_COLUMNS},transcript`)
    .eq("meeting_id", meetingId)
    .maybeSingle();
  if (error) throw error;
  return data ? { ...data, minutes: parseMinutes(data.minutes) } : null;
}

function parseMinutes(value: unknown): Minutes | null {
  if (!value) return null;
  if (typeof value === "string") {
    try {
      return JSON.parse(value) as Minutes;
    } catch {
      return null;
    }
  }
  return value as Minutes;
}

export function meetingTitle(meeting: Meeting): string {
  const topic = meeting.minutes?.discussion_topics?.[0]?.topic;
  return topic || `Meeting · ${formatDate(meeting.recorded_at)}`;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "Unknown date";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown date";
  return date.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function parseTranscript(text: string | undefined): TranscriptLine[] {
  const lines: TranscriptLine[] = [];
  for (const line of (text ?? "").split("\n")) {
    if (!line.trim()) continue;
    try {
      lines.push(JSON.parse(line));
    } catch {
      // skip malformed lines
    }
  }
  return lines;
}

export type ActionRow = { task: string; owner: string; deadline: string; meeting: Meeting };

export function collectActions(meetings: Meeting[]): ActionRow[] {
  return meetings.flatMap((meeting) =>
    (meeting.minutes?.action_items ?? []).map((item) => ({
      task: item.task ?? "",
      owner: item.owner || "Not specified",
      deadline: item.deadline || "Not specified",
      meeting,
    })),
  );
}

const STOPWORDS = new Set(
  "the and for are but not you all can had her was one our out has have what when where which who how why did does with from that this these those about into over than then them they their there will would could should any been being were your just also some more most other such only own same too very".split(" "),
);

export type Passage = { meeting_id: string; kind: string; chunk_index: number; content: string; recorded_at: string | null };

export async function askMeetings(question: string): Promise<Passage[]> {
  const words: string[] = [];
  for (const word of question.toLowerCase().match(/[\p{L}\p{N}]+/gu) ?? []) {
    if (word.length > 2 && !STOPWORDS.has(word) && !words.includes(word)) words.push(word);
  }
  if (!words.length) return [];
  const { data, error } = await supabase.rpc("search_meeting_chunks", {
    search_query: words.slice(0, 12).join(" | "),
    match_count: 8,
  });
  if (error) throw error;
  return (data ?? []) as Passage[];
}

export function friendlyError(error: unknown): string {
  const message = error instanceof Error ? error.message : String((error as { message?: string })?.message ?? error);
  if (/network|fetch/i.test(message)) return "No connection. Check your internet and try again.";
  return message;
}
