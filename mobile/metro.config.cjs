const { getDefaultConfig } = require('expo/metro-config')

// Expo resolves npm workspace packages and native modules from the repository root.
const config = getDefaultConfig(__dirname)
// Compose Icon loads Android vector drawables directly, as in t3code.
config.resolver.assetExts.push('xml')
module.exports = config
