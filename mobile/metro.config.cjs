const { getDefaultConfig } = require('expo/metro-config')

// Expo resolves npm workspace packages and native modules from the repository root.
module.exports = getDefaultConfig(__dirname)
