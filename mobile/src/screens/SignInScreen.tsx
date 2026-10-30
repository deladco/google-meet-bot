import React, { useState } from "react";
import { Image, KeyboardAvoidingView, Linking, Platform, ScrollView, Text, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { supabase, isConfigured } from "../supabase";
import { friendlyError } from "../data";
import { Button, ErrorText, Field, Muted } from "../ui";
import { colors, spacing } from "../theme";

export const PRIVACY_URL = process.env.EXPO_PUBLIC_PRIVACY_URL ?? "";

export default function SignInScreen() {
  const [mode, setMode] = useState<"in" | "up">("in");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function submit() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (mode === "in") {
        const { error: failure } = await supabase.auth.signInWithPassword({ email: email.trim(), password });
        if (failure) throw failure;
      } else {
        const { data, error: failure } = await supabase.auth.signUp({ email: email.trim(), password });
        if (failure) throw failure;
        if (!data.session) setNotice("Account created. Confirm the email we sent, then sign in.");
      }
    } catch (failure) {
      setError(friendlyError(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.background }}>
      <KeyboardAvoidingView behavior={Platform.OS === "ios" ? "padding" : undefined} style={{ flex: 1 }}>
        <ScrollView contentContainerStyle={{ padding: spacing.xl, gap: spacing.lg, flexGrow: 1, justifyContent: "center" }} keyboardShouldPersistTaps="handled">
          <View style={{ alignItems: "center", gap: spacing.md }}>
            <Image source={require("../../assets/icon.png")} style={{ width: 96, height: 96, borderRadius: 22 }} />
            <Text style={{ color: colors.text, fontSize: 30, fontWeight: "800" }}>Meet AI!</Text>
            <Text style={{ color: colors.muted, fontSize: 16, textAlign: "center" }}>
              Turn every meeting into clear notes, decisions and tasks.
            </Text>
          </View>
          {!isConfigured && <ErrorText message="The app is not configured. Set the Supabase URL and key and rebuild." />}
          <Field placeholder="Email" autoCapitalize="none" autoComplete="email" keyboardType="email-address" value={email} onChangeText={setEmail} />
          <Field
            placeholder={mode === "up" ? "Password (min 6 characters)" : "Password"}
            secureTextEntry
            autoComplete={mode === "in" ? "current-password" : "new-password"}
            value={password}
            onChangeText={setPassword}
          />
          <ErrorText message={error} />
          {notice && <Text style={{ color: colors.success }}>{notice}</Text>}
          <Button title={mode === "in" ? "Sign in" : "Create account"} onPress={submit} busy={busy} disabled={!email || password.length < 6} />
          <Button
            kind="secondary"
            title={mode === "in" ? "New here? Create an account" : "I already have an account"}
            onPress={() => {
              setMode(mode === "in" ? "up" : "in");
              setError(null);
            }}
          />
          {PRIVACY_URL ? (
            <Text style={{ color: colors.muted, textAlign: "center" }} onPress={() => Linking.openURL(PRIVACY_URL)}>
              Privacy policy
            </Text>
          ) : (
            <Muted>Your meetings are private to your account.</Muted>
          )}
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}
