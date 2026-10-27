// Test boundary for UIKit/SwiftUI/Compose controls. Native rendering is verified
// in the development build; component tests exercise application decisions.
const React = require('react')
const { Pressable, Text, TextInput, View } = require('react-native')
const h = React.createElement

exports.Button = ({ title, icon, busy, disabled, ...props }) =>
  h(
    Pressable,
    {
      ...props,
      accessibilityRole: 'button',
      accessibilityLabel: props.accessibilityLabel ?? title,
      disabled: disabled || busy,
      accessibilityState: {
        disabled: Boolean(disabled || busy),
        busy: Boolean(busy),
      },
    },
    icon,
    h(Text, null, busy ? `${title}…` : title),
  )
exports.IconButton = ({ icon, symbol, label, disabled, ...props }) =>
  h(
    Pressable,
    {
      ...props,
      accessibilityRole: 'button',
      accessibilityLabel: label,
      disabled,
      accessibilityState: {
        ...props.accessibilityState,
        disabled: Boolean(disabled),
      },
    },
    icon,
  )
exports.NativeAction = (props) => h(Pressable, props)
exports.NativeScreen = ({ title, left, right, children }) =>
  h(View, null, h(Text, null, title), left, right, children)
exports.ComposerSurface = ({ children }) => h(View, null, children)
exports.NativeTextField = React.forwardRef(({ label, ...props }, ref) =>
  h(TextInput, {
    ...props,
    accessibilityLabel: props.accessibilityLabel ?? label,
    ref,
  }),
)

exports.NativePicker = ({
  label,
  value,
  options,
  disabled,
  onChange,
  testID,
}) =>
  h(
    View,
    { testID: testID ?? `native-picker-${label}`, accessibilityLabel: label },
    ...options.map((option) =>
      h(
        Pressable,
        {
          key: option.value,
          accessibilityRole: 'button',
          accessibilityLabel: option.label,
          disabled: disabled || option.disabled,
          accessibilityState: {
            disabled: Boolean(disabled || option.disabled),
            selected: value === option.value,
          },
          onPress: () => onChange(option.value),
        },
        h(Text, null, option.label),
      ),
    ),
  )
exports.NativeOption = ({
  label,
  description,
  selected,
  multiple,
  disabled,
  onPress,
}) =>
  h(
    Pressable,
    {
      accessibilityRole: multiple ? 'checkbox' : 'radio',
      accessibilityLabel: [label, description].filter(Boolean).join(', '),
      accessibilityState: { checked: selected, disabled: Boolean(disabled) },
      disabled,
      onPress,
    },
    h(Text, null, label),
    description ? h(Text, null, description) : null,
  )

exports.NativeMenu = ({
  children,
  label,
  actions,
  onSelect,
  longPress,
  testID,
  style,
}) => {
  const [open, setOpen] = React.useState(false)
  return h(
    View,
    { style },
    h(
      Pressable,
      {
        testID,
        accessibilityRole: 'button',
        accessibilityLabel: label,
        [longPress ? 'onLongPress' : 'onPress']: () => setOpen(true),
      },
      children,
    ),
    ...(open
      ? actions.map((action) =>
          h(
            Pressable,
            {
              key: action.id,
              accessibilityRole: 'button',
              accessibilityLabel: action.title,
              disabled: action.disabled,
              onPress: () => {
                setOpen(false)
                onSelect(action.id)
              },
            },
            h(Text, null, action.title),
          ),
        )
      : []),
  )
}

exports.NativeList = ({ sections }) =>
  h(
    View,
    null,
    ...sections.map((section) =>
      h(
        View,
        { key: section.id, testID: `section-${section.id}` },
        section.title
          ? section.onExpandedChange
            ? h(
                Pressable,
                {
                  accessibilityRole: 'button',
                  accessibilityLabel: `${section.expanded ? 'Collapse' : 'Expand'} workspace ${section.title}`,
                  onPress: () => section.onExpandedChange(!section.expanded),
                },
                h(Text, null, section.title),
              )
            : h(Text, null, section.title)
          : null,
        section.onPress
          ? h(
              Pressable,
              {
                accessibilityRole: 'button',
                accessibilityLabel: `Open workspace ${section.title}`,
                onPress: section.onPress,
              },
              h(Text, null, section.title),
            )
          : null,
        ...(section.expanded === false
          ? []
          : section.rows.map((row) =>
              h(
                View,
                { key: row.id },
                row.onPress
                  ? h(
                      Pressable,
                      {
                        testID: row.testID ?? row.id,
                        accessibilityRole: 'button',
                        accessibilityLabel: [row.title, row.subtitle]
                          .filter(Boolean)
                          .join(', '),
                        accessibilityState: {
                          disabled: Boolean(row.disabled),
                          selected: Boolean(row.selected),
                        },
                        disabled: row.disabled,
                        onPress: row.onPress,
                      },
                      h(Text, null, row.title),
                      row.subtitle ? h(Text, null, row.subtitle) : null,
                    )
                  : h(Text, null, row.title),
                row.actions?.length
                  ? h(
                      exports.NativeMenu,
                      {
                        label: `Actions for ${row.title}`,
                        actions: row.actions,
                        onSelect: row.onAction,
                      },
                      h(Text, null, 'More'),
                    )
                  : null,
              ),
            )),
      ),
    ),
  )

exports.Drawer = ({
  visible,
  navigation,
  children,
  title,
  onOpen,
  onCreate,
  canCreate,
  menuActions,
  onMenuAction,
  onProfile,
  profileTitle,
}) =>
  h(
    View,
    null,
    h(exports.IconButton, {
      label: 'Open chats',
      onPress: onOpen,
      testID: 'main-menu',
    }),
    h(exports.Button, {
      title: 'New chat',
      onPress: onCreate,
      disabled: !canCreate,
      testID: 'new-chat',
    }),
    h(
      exports.NativeMenu,
      {
        label: `More for ${title}`,
        actions: menuActions,
        onSelect: onMenuAction,
        testID: 'chat-menu',
      },
      h(Text, null, 'More'),
    ),
    h(
      View,
      {
        testID: 'conversation-drawer',
        accessibilityElementsHidden: !visible,
        importantForAccessibility: visible ? 'auto' : 'no-hide-descendants',
        style: { display: visible ? 'flex' : 'none' },
      },
      navigation,
      h(exports.Button, {
        title: profileTitle,
        onPress: onProfile,
        testID: 'drawer-profile',
      }),
    ),
    children,
  )

exports.NativeSheet = ({ visible, children, testID }) =>
  visible ? h(View, { testID }, children) : null

exports.ModalFrame = ({ title, onClose, children }) =>
  h(
    View,
    null,
    h(Text, null, title),
    onClose
      ? h(exports.IconButton, { label: 'Close', onPress: onClose })
      : null,
    children,
  )
