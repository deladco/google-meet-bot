import React, { useCallback, useEffect, useState } from "react";
import { Alert, ScrollView, Switch, Text, View } from "react-native";
import { useFocusEffect } from "@react-navigation/native";
import {
  AudioModule,
  RecordingPresets,
  setAudioModeAsync,
  useAudioRecorder,
  useAudioRecorderState,
} from "expo-audio";
import * as DocumentPicker from "expo-document-picker";
import { supabase } from "../supabase";
import { friendlyError } from "../data";
import { Button, Card, ErrorText, Muted, styles } from "../ui";
import { colors, spacing } from "../theme";

type AudioJob = { id: string; status: string; message: string | null; filename: string; created_at: string };

const STATUS_TEXT: Record<string, string> = {
  queued: "Waiting to be processed",
  processing: "Transcribing and writing your summary",
  done: "Ready in My Meetings",
  failed: "Failed",
};

function formatClock(ms: number) {
  const total = Math.floor(ms / 1000);
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

export default function RecordScreen() {
  const recorder = useAudioRecorder(RecordingPresets.HIGH_QUALITY);
  const state = useAudioRecorderState(recorder, 500);
  const [consent, setConsent] = useState(false);
  const [uri, setUri] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [jobs, setJobs] = useState<AudioJob[]>([]);

  const loadJobs = useCallback(async () => {
    const { data } = await supabase
      .from("audio_jobs")
      .select("id,status,message,filename,created_at")
      .order("created_at", { ascending: false })
      .limit(5);
    setJobs((data ?? []) as AudioJob[]);
  }, []);

  useFocusEffect(
    useCallback(() => {
      loadJobs();
      const timer = setInterval(loadJobs, 8000);
      return () => clearInterval(timer);
    }, [loadJobs]),
  );

  useEffect(() => {
    // Release the microphone if the screen goes away mid-recording.
    return () => {
      if (recorder.isRecording) recorder.stop().catch(() => undefined);
    };
  }, [recorder]);

  async function start() {
    setError(null);
    setUri(null);
    try {
      const permission = await AudioModule.requestRecordingPermissionsAsync();
      if (!permission.granted) {
        setError("Microphone access is needed to record. Enable it in Settings > Meet AI.");
        return;
      }
      await setAudioModeAsync({ allowsRecording: true, playsInSilentMode: true });
      await recorder.prepareToRecordAsync();
      recorder.record();
    } catch (failure) {
      setError(friendlyError(failure));
    }
  }

  async function stop() {
    try {
      await recorder.stop();
      await setAudioModeAsync({ allowsRecording: false });
      setUri(recorder.uri);
    } catch (failure) {
      setError(friendlyError(failure));
    }
  }

  async function pickFile() {
    setError(null);
    const result = await DocumentPicker.getDocumentAsync({ type: "audio/*", copyToCacheDirectory: true });
    if (!result.canceled && result.assets[0]) setUri(result.assets[0].uri);
  }

  async function upload() {
    if (!uri || !consent) return;
    setBusy(true);
    setError(null);
    try {
      const { data: userData, error: userError } = await supabase.auth.getUser();
      if (userError || !userData.user) throw userError ?? new Error("Please sign in again.");
      const user = userData.user;
      const extension = (uri.split(".").pop() ?? "m4a").toLowerCase().slice(0, 5);
      const filename = `recording-${Date.now()}.${extension}`;
      const path = `${user.id}/${filename}`;

      setStep("Uploading recording");
      const body = await (await fetch(uri)).arrayBuffer();
      const { error: uploadError } = await supabase.storage
        .from("recordings")
        .upload(path, body, { contentType: extension === "m4a" ? "audio/mp4" : `audio/${extension}` });
      if (uploadError) throw uploadError;

      setStep("Queueing for processing");
      const { error: jobError } = await supabase
        .from("audio_jobs")
        .insert({ storage_path: path, filename, user_email: user.email });
      if (jobError) throw jobError;

      setUri(null);
      setConsent(false);
      await loadJobs();
      Alert.alert("Uploaded", "Your summary will appear in My Meetings in a few minutes.");
    } catch (failure) {
      setError(friendlyError(failure));
    } finally {
      setBusy(false);
      setStep(null);
    }
  }

  const recording = state.isRecording;

  return (
    <ScrollView style={{ backgroundColor: colors.background }} contentContainerStyle={{ padding: spacing.lg, gap: spacing.lg }}>
      <Card>
        <Text style={styles.title}>Record a conversation</Text>
        <Muted>Only record when everyone present has agreed. The audio is deleted after it is transcribed.</Muted>
        <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.md, marginTop: spacing.sm }}>
          <Switch value={consent} onValueChange={setConsent} trackColor={{ true: colors.primary }} />
          <Text style={[styles.body, { flex: 1 }]}>Everyone in this conversation has agreed to be recorded.</Text>
        </View>
      </Card>

      <Text style={{ color: colors.text, fontSize: 44, fontWeight: "200", textAlign: "center" }}>
        {formatClock(state.durationMillis)}
      </Text>

      {recording ? (
        <Button kind="danger" title="Stop recording" onPress={stop} />
      ) : (
        <Button title={uri ? "Record again" : "Start recording"} onPress={start} disabled={!consent || busy} />
      )}
      {!recording && !uri && <Button kind="secondary" title="Choose an audio file" onPress={pickFile} disabled={!consent || busy} />}

      {uri && !recording && (
        <Card>
          <Text style={styles.title}>Recording ready</Text>
          <Muted>{step ?? "Upload it to create your meeting notes."}</Muted>
          <View style={{ height: spacing.sm }} />
          <Button title="Upload and create notes" onPress={upload} busy={busy} disabled={!consent} />
        </Card>
      )}
      <ErrorText message={error} />

      {jobs.length > 0 && (
        <View style={{ gap: spacing.sm }}>
          <Text style={{ color: colors.text, fontSize: 18, fontWeight: "700" }}>Recent uploads</Text>
          {jobs.map((job) => (
            <Card key={job.id}>
              <Text style={styles.title}>{STATUS_TEXT[job.status] ?? job.status}</Text>
              <Muted>{new Date(job.created_at).toLocaleString()}</Muted>
              {job.status === "failed" && !!job.message && <Muted>{job.message}</Muted>}
            </Card>
          ))}
        </View>
      )}
    </ScrollView>
  );
}
