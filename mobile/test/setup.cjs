jest.mock('react-native-safe-area-context', () => require('react-native-safe-area-context/jest/mock'))
jest.mock('react-native-keyboard-controller', () => require('react-native-keyboard-controller/jest'))
jest.mock('react-native-enriched-markdown', () => ({
  EnrichedMarkdownText: ({ markdown, ...props }) => {
    const React = require('react')
    const { Text } = require('react-native')
    return React.createElement(Text, props, markdown)
  },
}))

jest.mock('expo-video', () => ({
  useVideoPlayer: (source) => ({ source, play: jest.fn(), pause: jest.fn() }),
  VideoView: (props) => {
    const React = require('react')
    const { View } = require('react-native')
    return React.createElement(View, props)
  },
}))
