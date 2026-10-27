require('react-native-gesture-handler/jestSetup')
require('react-native-reanimated').setUpTests()

jest.mock('react-native-safe-area-context', () => {
  const mock = require('react-native-safe-area-context/jest/mock')
  return mock.default ?? mock
})
jest.mock('react-native-keyboard-controller', () =>
  require('react-native-keyboard-controller/jest'),
)
jest.mock('../src/ui/controls', () => require('./native-ui.cjs'))
jest.mock('../src/ui/ModalFrame', () => require('./native-ui.cjs'))
jest.mock('../src/ui/NativeScreen', () => require('./native-ui.cjs'))
jest.mock('../src/ui/ComposerSurface', () => require('./native-ui.cjs'))
jest.mock('../src/ui/NativeMenu', () => require('./native-ui.cjs'))
jest.mock('../src/ui/NativeList', () => require('./native-ui.cjs'))
jest.mock('../src/ui/selection', () => require('./native-ui.cjs'))
jest.mock('../src/ui/NativeTextField', () => require('./native-ui.cjs'))
jest.mock('../src/ui/Drawer', () => require('./native-ui.cjs'))
jest.mock('../src/ui/NativeSheet', () => require('./native-ui.cjs'))
jest.mock('@expo/ui/community/segmented-control', () => ({
  SegmentedControl: () => null,
}))
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
