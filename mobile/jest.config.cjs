module.exports = {
  preset: 'jest-expo',
  testMatch: ['<rootDir>/src/**/*.test.tsx'],
  setupFilesAfterEnv: ['<rootDir>/test/setup.cjs'],
  // Worklets run their JavaScript implementation instead of the native runtime.
  resolver: 'react-native-worklets/jest/resolver',
  transformIgnorePatterns: [
    'node_modules/(?!((jest-)?react-native|@react-native(-community)?|@opencuria|@legendapp|expo(nent)?|@expo(nent)?/.*|expo-.*|@react-navigation/.*|react-navigation|react-native-.*|lucide-react-native|marked|remend)/)',
  ],
}
