import "react-native-url-polyfill/auto";
import * as SecureStore from "expo-secure-store";
import { createClient } from "@supabase/supabase-js";

const url = process.env.EXPO_PUBLIC_SUPABASE_URL ?? "";
const key = process.env.EXPO_PUBLIC_SUPABASE_KEY ?? "";

export const isConfigured = Boolean(url && key);

// SecureStore values are limited to ~2 KB, so a session is stored in chunks.
const CHUNK = 1800;
const storage = {
  async getItem(name: string) {
    const count = Number(await SecureStore.getItemAsync(`${name}.n`));
    if (!count) return null;
    let value = "";
    for (let i = 0; i < count; i++) value += (await SecureStore.getItemAsync(`${name}.${i}`)) ?? "";
    return value;
  },
  async setItem(name: string, value: string) {
    const count = Math.ceil(value.length / CHUNK);
    for (let i = 0; i < count; i++) await SecureStore.setItemAsync(`${name}.${i}`, value.slice(i * CHUNK, (i + 1) * CHUNK));
    await SecureStore.setItemAsync(`${name}.n`, String(count));
  },
  async removeItem(name: string) {
    const count = Number(await SecureStore.getItemAsync(`${name}.n`));
    for (let i = 0; i < count; i++) await SecureStore.deleteItemAsync(`${name}.${i}`);
    await SecureStore.deleteItemAsync(`${name}.n`);
  },
};

export const supabase = createClient(url || "https://example.invalid", key || "missing", {
  auth: { storage, autoRefreshToken: true, persistSession: true, detectSessionInUrl: false },
});
