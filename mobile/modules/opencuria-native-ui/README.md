# Native modal toolbar

Expose Android's actual AppCompat `Toolbar` in system modal dialogs. A native-stack screen cannot mount beneath React Native's Android modal root, and this SDK's Expo UI bridge has no TopAppBar export. This small local Expo module supplies the platform toolbar instead of drawing an imitation header. It contains no app or chat logic. iOS modal navigation bars use SwiftUI's `NavigationStack`.

Expo autolinks local modules from `mobile/modules/`; rebuild the Android development client after changes.
