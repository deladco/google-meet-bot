import React from "react";
import { ActivityIndicator, View } from "react-native";
import { StatusBar } from "expo-status-bar";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { DarkTheme, NavigationContainer } from "@react-navigation/native";
import { createBottomTabNavigator } from "@react-navigation/bottom-tabs";
import { createNativeStackNavigator } from "@react-navigation/native-stack";
import { Ionicons } from "@expo/vector-icons";
import { AuthProvider, useAuth } from "./src/auth";
import { colors } from "./src/theme";
import SignInScreen from "./src/screens/SignInScreen";
import MeetingsScreen from "./src/screens/MeetingsScreen";
import MeetingDetailScreen from "./src/screens/MeetingDetailScreen";
import ActionsScreen from "./src/screens/ActionsScreen";
import AskScreen from "./src/screens/AskScreen";
import RecordScreen from "./src/screens/RecordScreen";
import SettingsScreen from "./src/screens/SettingsScreen";

const Tabs = createBottomTabNavigator();
const Stack = createNativeStackNavigator();

const theme = {
  ...DarkTheme,
  colors: { ...DarkTheme.colors, background: colors.background, card: colors.surface, text: colors.text, border: colors.border, primary: colors.primary },
};

const ICONS: Record<string, keyof typeof Ionicons.glyphMap> = {
  "My Meetings": "documents-outline",
  Record: "mic-outline",
  "Ask AI": "sparkles-outline",
  "Action Items": "checkbox-outline",
  Settings: "settings-outline",
};

function MainTabs() {
  return (
    <Tabs.Navigator
      screenOptions={({ route }) => ({
        tabBarIcon: ({ color, size }) => <Ionicons name={ICONS[route.name]} size={size} color={color} />,
        tabBarActiveTintColor: colors.primary,
        tabBarInactiveTintColor: colors.muted,
      })}
    >
      <Tabs.Screen name="My Meetings" component={MeetingsScreen} />
      <Tabs.Screen name="Record" component={RecordScreen} />
      <Tabs.Screen name="Ask AI" component={AskScreen} />
      <Tabs.Screen name="Action Items" component={ActionsScreen} />
      <Tabs.Screen name="Settings" component={SettingsScreen} />
    </Tabs.Navigator>
  );
}

function Root() {
  const { session, loading } = useAuth();
  if (loading) {
    return (
      <View style={{ flex: 1, backgroundColor: colors.background, justifyContent: "center" }}>
        <ActivityIndicator color={colors.primary} />
      </View>
    );
  }
  if (!session) return <SignInScreen />;
  return (
    <Stack.Navigator>
      <Stack.Screen name="Main" component={MainTabs} options={{ headerShown: false }} />
      <Stack.Screen name="MeetingDetail" component={MeetingDetailScreen} options={{ title: "Meeting", headerBackTitle: "Back" }} />
    </Stack.Navigator>
  );
}

export default function App() {
  return (
    <SafeAreaProvider>
      <AuthProvider>
        <NavigationContainer theme={theme}>
          <StatusBar style="light" />
          <Root />
        </NavigationContainer>
      </AuthProvider>
    </SafeAreaProvider>
  );
}
