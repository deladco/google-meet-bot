import React, { useCallback, useMemo, useState } from "react";
import { FlatList, RefreshControl, Text, View } from "react-native";
import { useFocusEffect, useNavigation } from "@react-navigation/native";
import { Card, EmptyState, ErrorText, Field, Muted, styles } from "../ui";
import { Meeting, formatDate, friendlyError, listMeetings, meetingTitle } from "../data";
import { colors, spacing } from "../theme";

export default function MeetingsScreen() {
  const navigation = useNavigation<any>();
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  const load = useCallback(async () => {
    setError(null);
    try {
      setMeetings(await listMeetings());
    } catch (failure) {
      setError(friendlyError(failure));
    } finally {
      setLoading(false);
    }
  }, []);

  useFocusEffect(
    useCallback(() => {
      load();
    }, [load]),
  );

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return meetings;
    return meetings.filter((meeting) =>
      `${meetingTitle(meeting)} ${meeting.minutes?.summary ?? ""} ${meeting.notes ?? ""}`.toLowerCase().includes(needle),
    );
  }, [meetings, query]);

  return (
    <View style={{ flex: 1, backgroundColor: colors.background }}>
      <FlatList
        data={shown}
        keyExtractor={(meeting) => meeting.meeting_id}
        contentContainerStyle={{ padding: spacing.lg, gap: spacing.md }}
        refreshControl={<RefreshControl refreshing={loading} onRefresh={load} tintColor={colors.primary} />}
        ListHeaderComponent={
          <View style={{ gap: spacing.md }}>
            <Field placeholder="Search meetings" value={query} onChangeText={setQuery} clearButtonMode="while-editing" />
            <ErrorText message={error} />
          </View>
        }
        ListEmptyComponent={
          loading ? undefined : meetings.length === 0 ? (
            <EmptyState
              title="No meetings yet"
              body="Open the Record tab, record a conversation and give consent. Your summary and action items appear here when processing finishes."
            />
          ) : (
            <EmptyState title="No meetings match" body="Try a different word." />
          )
        }
        renderItem={({ item }) => (
          <Card onPress={() => navigation.navigate("MeetingDetail", { id: item.meeting_id })}>
            <Text style={styles.title}>{meetingTitle(item)}</Text>
            <Muted>
              {formatDate(item.recorded_at)}
              {item.minutes
                ? ` · ${item.minutes.decisions?.length ?? 0} decisions · ${item.minutes.action_items?.length ?? 0} actions`
                : ` · ${(item.status ?? "unknown").replace(/_/g, " ")}`}
            </Muted>
          </Card>
        )}
      />
    </View>
  );
}
