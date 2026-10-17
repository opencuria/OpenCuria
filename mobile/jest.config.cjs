module.exports = {
  preset: 'jest-expo',
  testMatch: ['<rootDir>/src/**/*.test.tsx'],
  setupFilesAfterEnv: ['<rootDir>/test/setup.cjs'],
  transformIgnorePatterns: [
    'node_modules/(?!((jest-)?react-native|@react-native(-community)?|@opencuria|@legendapp|expo(nent)?|@expo(nent)?/.*|expo-.*|@react-navigation/.*|react-navigation|react-native-.*|lucide-react-native|marked|remend)/)',
  ],
}
