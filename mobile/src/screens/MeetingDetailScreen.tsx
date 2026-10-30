import React, { useEffect, useState } from "react";
import { ActivityIndicator, ScrollView, Text, View } from "react-native";
import { useNavigation, useRoute } from "@react-navigation/native";
import { Card, ErrorText, Muted, styles } from "../ui";
import { Meeting, formatDate, friendlyError, getMeeting, meetingTitle, parseTranscript } from "../data";
import { colors, spacing } from "../theme";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <View style={{ gap: spacing.sm }}>
      <Text style={{ color: colors.text, fontSize: 18, fontWeight: "700" }}>{title}</Text>
      {children}
    </View>
  );
}

export default function MeetingDetailScreen() {
  const route = useRoute<any>();
  const navigation = useNavigation<any>();
  const [meeting, setMeeting] = useState<Meeting | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showTranscript, setShowTranscript] = useState(false);

  useEffect(() => {
    getMeeting(route.params.id)
      .then((value) => {
        setMeeting(value);
        if (value) navigation.setOptions({ title: meetingTitle(value) });
      })
      .catch((failure) => setError(friendlyError(failure)))
      .finally(() => setLoading(false));
  }, [route.params.id, navigation]);

  if (loading) return <ActivityIndicator style={{ marginTop: 40 }} color={colors.primary} />;
  if (!meeting) return <View style={{ padding: spacing.lg }}><ErrorText message={error ?? "Meeting not found."} /></View>;

  const minutes = meeting.minutes;
  const lines = parseTranscript(meeting.transcript);

  return (
    <ScrollView style={{ backgroundColor: colors.background }} contentContainerStyle={{ padding: spacing.lg, gap: spacing.xl }}>
      <Muted>{formatDate(meeting.recorded_at)}</Muted>
      <Section title="Summary">
        <Text style={styles.body}>{minutes?.summary || "A summary has not been generated for this meeting yet."}</Text>
      </Section>
      <Section title={`Decisions (${minutes?.decisions?.length ?? 0})`}>
        {minutes?.decisions?.length ? (
          minutes.decisions.map((item, index) => (
            <Text key={index} style={styles.body}>• {item.decision}</Text>
          ))
        ) : (
          <Muted>No decisions were recorded.</Muted>
        )}
      </Section>
      <Section title={`Action items (${minutes?.action_items?.length ?? 0})`}>
        {minutes?.action_items?.length ? (
          minutes.action_items.map((item, index) => (
            <Card key={index}>
              <Text style={styles.title}>{item.task}</Text>
              <Muted>Owner: {item.owner || "Not specified"} · Deadline: {item.deadline || "Not specified"}</Muted>
            </Card>
          ))
        ) : (
          <Muted>No action items were captured.</Muted>
        )}
      </Section>
      {!!minutes?.unresolved_questions?.length && (
        <Section title="Open questions">
          {minutes.unresolved_questions.map((item, index) => (
            <Text key={index} style={styles.body}>• {item.question}</Text>
          ))}
        </Section>
      )}
      <Section title={`Transcript (${lines.length} lines)`}>
        <Card onPress={() => setShowTranscript(!showTranscript)}>
          <Text style={styles.title}>{showTranscript ? "Hide transcript" : "Show transcript"}</Text>
        </Card>
        {showTranscript &&
          lines.map((line, index) => (
            <Text key={index} style={styles.body}>
              <Text style={{ color: colors.primary, fontWeight: "700" }}>{line.speaker ?? "Unknown"} </Text>
              {line.text}
            </Text>
          ))}
      </Section>
    </ScrollView>
  );
}
