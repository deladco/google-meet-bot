import React, { useState } from "react";
import { KeyboardAvoidingView, Platform, ScrollView, Text, View } from "react-native";
import { useNavigation } from "@react-navigation/native";
import { Button, Card, EmptyState, ErrorText, Field, Muted, styles } from "../ui";
import { Passage, askMeetings, formatDate, friendlyError } from "../data";
import { colors, spacing } from "../theme";

const SUGGESTIONS = ["What decisions did we make?", "What deadlines were discussed?", "Who owns which tasks?"];

export default function AskScreen() {
  const navigation = useNavigation<any>();
  const [question, setQuestion] = useState("");
  const [asked, setAsked] = useState<string | null>(null);
  const [passages, setPassages] = useState<Passage[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function ask(text: string) {
    const value = text.trim();
    if (!value) return;
    setQuestion(value);
    setBusy(true);
    setError(null);
    try {
      setPassages(await askMeetings(value));
      setAsked(value);
    } catch (failure) {
      setError(friendlyError(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <KeyboardAvoidingView behavior={Platform.OS === "ios" ? "padding" : undefined} style={{ flex: 1, backgroundColor: colors.background }}>
      <ScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.md }} keyboardShouldPersistTaps="handled">
        <Muted>Ask in plain words. Results come from your own meetings and show where they were found.</Muted>
        <Field placeholder="What did we decide about the launch?" value={question} onChangeText={setQuestion} returnKeyType="search" onSubmitEditing={() => ask(question)} />
        <Button title="Ask" onPress={() => ask(question)} busy={busy} disabled={!question.trim()} />
        <ErrorText message={error} />
        {asked === null && !busy && (
          <View style={{ gap: spacing.sm }}>
            <Muted>Try asking</Muted>
            {SUGGESTIONS.map((suggestion) => (
              <Button key={suggestion} kind="secondary" title={suggestion} onPress={() => ask(suggestion)} />
            ))}
          </View>
        )}
        {asked !== null && passages.length === 0 && !busy && (
          <EmptyState title="Nothing found" body="Try different words, or record a meeting first." />
        )}
        {passages.map((passage, index) => (
          <Card key={`${passage.meeting_id}-${passage.kind}-${passage.chunk_index}-${index}`} onPress={() => navigation.navigate("MeetingDetail", { id: passage.meeting_id })}>
            <Muted>{formatDate(passage.recorded_at)} · {passage.kind === "notes" ? "Notes" : "Transcript"}</Muted>
            <Text style={styles.body} numberOfLines={8}>{passage.content}</Text>
          </Card>
        ))}
      </ScrollView>
    </KeyboardAvoidingView>
  );
}
