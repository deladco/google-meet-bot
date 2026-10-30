import React, { useCallback, useMemo, useState } from "react";
import { FlatList, Share, Text, View } from "react-native";
import { useFocusEffect, useNavigation } from "@react-navigation/native";
import { Button, Card, EmptyState, ErrorText, Field, Muted, styles } from "../ui";
import { ActionRow, collectActions, friendlyError, listMeetings, meetingTitle, formatDate } from "../data";
import { colors, spacing } from "../theme";

export default function ActionsScreen() {
  const navigation = useNavigation<any>();
  const [rows, setRows] = useState<ActionRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  useFocusEffect(
    useCallback(() => {
      listMeetings()
        .then((meetings) => setRows(collectActions(meetings)))
        .catch((failure) => setError(friendlyError(failure)))
        .finally(() => setLoading(false));
    }, []),
  );

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return needle ? rows.filter((row) => `${row.task} ${row.owner}`.toLowerCase().includes(needle)) : rows;
  }, [rows, query]);

  function exportList() {
    const text = shown
      .map((row) => `- ${row.task} (Owner: ${row.owner}, Deadline: ${row.deadline}) — ${meetingTitle(row.meeting)}`)
      .join("\n");
    Share.share({ message: text });
  }

  return (
    <View style={{ flex: 1, backgroundColor: colors.background }}>
      <FlatList
        data={shown}
        keyExtractor={(_row, index) => String(index)}
        contentContainerStyle={{ padding: spacing.lg, gap: spacing.md }}
        ListHeaderComponent={
          <View style={{ gap: spacing.md }}>
            <Field placeholder="Search tasks or owners" value={query} onChangeText={setQuery} clearButtonMode="while-editing" />
            <ErrorText message={error} />
            {shown.length > 0 && <Button kind="secondary" title="Share list" onPress={exportList} />}
          </View>
        }
        ListEmptyComponent={
          loading ? undefined : (
            <EmptyState
              title="No action items yet"
              body="Tasks assigned in your meetings are collected here after a meeting's summary is generated."
            />
          )
        }
        renderItem={({ item }) => (
          <Card onPress={() => navigation.navigate("MeetingDetail", { id: item.meeting.meeting_id })}>
            <Text style={styles.title}>{item.task}</Text>
            <Muted>Owner: {item.owner} · Deadline: {item.deadline}</Muted>
            <Muted>{meetingTitle(item.meeting)} · {formatDate(item.meeting.recorded_at)}</Muted>
          </Card>
        )}
      />
    </View>
  );
}
