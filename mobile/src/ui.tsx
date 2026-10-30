import React from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, TextInput, TextInputProps, View, ViewStyle } from "react-native";
import { colors, spacing } from "./theme";

export function Card({ children, onPress, style }: { children: React.ReactNode; onPress?: () => void; style?: ViewStyle }) {
  const content = (state: { pressed: boolean }) => [
    styles.card,
    state.pressed && onPress ? { backgroundColor: colors.surfaceHover } : null,
    style,
  ];
  return onPress ? (
    <Pressable onPress={onPress} style={content} accessibilityRole="button">
      {children}
    </Pressable>
  ) : (
    <View style={content({ pressed: false })}>{children}</View>
  );
}

export function Button({
  title,
  onPress,
  kind = "primary",
  busy,
  disabled,
}: {
  title: string;
  onPress: () => void;
  kind?: "primary" | "secondary" | "danger";
  busy?: boolean;
  disabled?: boolean;
}) {
  const background = kind === "primary" ? colors.primary : kind === "danger" ? "transparent" : colors.surface;
  const textColor = kind === "primary" ? colors.primaryText : kind === "danger" ? colors.danger : colors.text;
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled || busy}
      accessibilityRole="button"
      style={({ pressed }) => [
        styles.button,
        { backgroundColor: background, borderColor: kind === "primary" ? colors.primary : kind === "danger" ? colors.danger : colors.border },
        pressed && { opacity: 0.75 },
        (disabled || busy) && { opacity: 0.5 },
      ]}
    >
      {busy ? <ActivityIndicator color={textColor} /> : <Text style={[styles.buttonText, { color: textColor }]}>{title}</Text>}
    </Pressable>
  );
}

export function Field(props: TextInputProps) {
  return <TextInput placeholderTextColor={colors.muted} {...props} style={[styles.input, props.style]} />;
}

export function Heading({ children }: { children: React.ReactNode }) {
  return <Text style={styles.heading}>{children}</Text>;
}

export function Muted({ children }: { children: React.ReactNode }) {
  return <Text style={styles.muted}>{children}</Text>;
}

export function EmptyState({ title, body, action }: { title: string; body: string; action?: React.ReactNode }) {
  return (
    <View style={styles.empty}>
      <Text style={styles.emptyTitle}>{title}</Text>
      <Text style={[styles.muted, { textAlign: "center" }]}>{body}</Text>
      {action}
    </View>
  );
}

export function ErrorText({ message }: { message: string | null }) {
  return message ? <Text style={styles.error}>{message}</Text> : null;
}

export const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.surface,
    borderColor: colors.border,
    borderWidth: 1,
    borderRadius: 14,
    padding: spacing.lg,
    gap: spacing.xs,
  },
  button: {
    minHeight: 48,
    borderRadius: 12,
    borderWidth: 1,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: spacing.lg,
  },
  buttonText: { fontSize: 16, fontWeight: "600" },
  input: {
    minHeight: 48,
    borderRadius: 12,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surface,
    color: colors.text,
    paddingHorizontal: spacing.lg,
    fontSize: 16,
  },
  heading: { color: colors.text, fontSize: 22, fontWeight: "700" },
  muted: { color: colors.muted, fontSize: 14, lineHeight: 20 },
  empty: { alignItems: "center", gap: spacing.md, padding: spacing.xl, marginTop: spacing.xl },
  emptyTitle: { color: colors.text, fontSize: 18, fontWeight: "600", textAlign: "center" },
  error: { color: colors.danger, fontSize: 14 },
  body: { color: colors.text, fontSize: 16, lineHeight: 23 },
  title: { color: colors.text, fontSize: 16, fontWeight: "600" },
});
