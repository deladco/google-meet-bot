import React, { useState } from "react";
import { Alert, Linking, ScrollView, Text } from "react-native";
import Constants from "expo-constants";
import { supabase } from "../supabase";
import { useAuth } from "../auth";
import { friendlyError } from "../data";
import { Button, Card, ErrorText, Muted, styles } from "../ui";
import { PRIVACY_URL } from "./SignInScreen";
import { colors, spacing } from "../theme";

export default function SettingsScreen() {
  const { session } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function confirmDelete() {
    Alert.alert(
      "Delete account?",
      "This permanently deletes your account, meetings and notes. It cannot be undone.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Delete",
          style: "destructive",
          onPress: async () => {
            setBusy(true);
            setError(null);
            const { error: failure } = await supabase.rpc("delete_my_account");
            if (failure) {
              setError(friendlyError(failure));
              setBusy(false);
              return;
            }
            await supabase.auth.signOut();
          },
        },
      ],
    );
  }

  return (
    <ScrollView style={{ backgroundColor: colors.background }} contentContainerStyle={{ padding: spacing.lg, gap: spacing.lg }}>
      <Card>
        <Text style={styles.title}>Account</Text>
        <Muted>Signed in as {session?.user.email}</Muted>
      </Card>
      <Button kind="secondary" title="Sign out" onPress={() => supabase.auth.signOut()} />
      {!!PRIVACY_URL && <Button kind="secondary" title="Privacy policy" onPress={() => Linking.openURL(PRIVACY_URL)} />}
      <Button kind="danger" title="Delete my account" onPress={confirmDelete} busy={busy} />
      <ErrorText message={error} />
      <Muted>Meet AI! {Constants.expoConfig?.version}</Muted>
    </ScrollView>
  );
}
