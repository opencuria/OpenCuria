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
  accessibilityLabel,
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
        accessibilityLabel: accessibilityLabel ?? label,
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

exports.NativeContextMenu = ({ children, label, actions, onSelect, fitContent }) => {
  const [open, setOpen] = React.useState(false)
  if (fitContent) return h(
    View,
    null,
    children(() => setOpen(true)),
    ...(open ? actions.map((action) => h(
      Pressable,
      {
        key: action.id,
        accessibilityRole: 'button',
        accessibilityLabel: action.title,
        disabled: action.disabled,
        onPress: () => { setOpen(false); onSelect(action.id) },
      },
      h(Text, null, action.title),
    )) : []),
  )
  return h(
    View,
    null,
    children(),
    h(
      exports.NativeMenu,
      { label: `Actions for ${label}`, actions, onSelect },
      h(Text, null, 'More'),
    ),
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
  onTools,
  canOpenTools,
  menuActions,
  onMenuAction,
}) =>
  h(
    View,
    null,
    h(exports.IconButton, {
      label: 'Open sidebar',
      onPress: onOpen,
      testID: 'main-menu',
    }),
    h(exports.Button, {
      title: 'New chat',
      onPress: onCreate,
      disabled: !canCreate,
      testID: 'new-chat',
    }),
    h(exports.IconButton, {
      label: 'Git, Terminal & Files',
      onPress: onTools,
      disabled: !canOpenTools,
      testID: 'workspace-tools',
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
    ),
    children,
  )

exports.NativeSheet = ({ visible, children, testID }) =>
  visible ? h(View, { testID }, children) : null

exports.ModalFrame = ({
  title,
  onClose,
  actions,
  onAction,
  primaryAction,
  search,
  children,
}) =>
  h(
    View,
    null,
    h(Text, null, title),
    onClose
      ? h(exports.IconButton, { label: 'Close', onPress: onClose })
      : null,
    search
      ? h(TextInput, {
          accessibilityLabel: search.placeholder,
          value: search.value,
          onChangeText: search.onChange,
        })
      : null,
    primaryAction
      ? h(exports.Button, {
          title: primaryAction.title,
          onPress: primaryAction.onPress,
          disabled: primaryAction.disabled,
          busy: primaryAction.busy,
          testID: primaryAction.testID,
        })
      : null,
    actions?.length
      ? h(
          exports.NativeMenu,
          { label: `More for ${title}`, actions, onSelect: onAction },
          h(Text, null, 'More'),
        )
      : null,
    // Native bars are not siblings of the modal content's accessibility root.
    h(View, null, children),
  )

function formRow(row) {
  const key = row.id
  switch (row.kind) {
    case 'item': {
      const menu = [
        ...(row.leadingSwipe ?? []),
        ...(row.actions ?? []),
        ...(row.trailingSwipe ?? []),
      ].filter(
        (action, index, all) =>
          all.findIndex((entry) => entry.id === action.id) === index,
      )
      return h(
        View,
        { key },
        h(
          Pressable,
          {
            testID: row.testID ?? row.id,
            accessibilityRole: 'button',
            accessibilityLabel: [row.title, row.subtitle]
              .filter(Boolean)
              .join(', '),
            accessibilityState: {
              disabled: Boolean(row.disabled || !row.onPress),
              selected: Boolean(row.selected),
              busy: Boolean(row.busy),
            },
            disabled: row.disabled || !row.onPress,
            onPress: row.onPress,
          },
          h(Text, null, row.title),
          row.subtitle ? h(Text, null, row.subtitle) : null,
          row.badge ? h(Text, null, row.badge) : null,
        ),
        row.onInfo
          ? h(exports.IconButton, {
              label: row.infoLabel ?? `Details for ${row.title}`,
              onPress: row.onInfo,
            })
          : null,
        menu.length
          ? h(
              exports.NativeMenu,
              {
                label: `Actions for ${row.title}`,
                actions: menu,
                onSelect: row.onAction,
              },
              h(Text, null, 'More'),
            )
          : null,
      )
    }
    case 'header':
      return h(
        View,
        { key, testID: row.testID ?? row.id },
        h(Text, null, row.title),
        row.subtitle ? h(Text, null, row.subtitle) : null,
        ...row.actions.map((action) =>
          h(exports.Button, {
            key: action.id,
            title: action.title,
            disabled: action.disabled,
            testID: `${row.id}-${action.id}`,
            onPress: () => row.onAction(action.id),
          }),
        ),
      )
    case 'text':
      return h(TextInput, {
        key,
        testID: row.testID ?? row.id,
        accessibilityLabel: row.label,
        placeholder: row.placeholder,
        value: row.value,
        editable: !row.disabled,
        onChangeText: row.onChange,
        onSubmitEditing: row.onSubmit,
      })
    case 'toggle':
      return h(
        Pressable,
        {
          key,
          testID: row.testID ?? row.id,
          accessibilityRole: 'switch',
          accessibilityLabel: row.label,
          accessibilityHint: row.subtitle,
          accessibilityState: {
            checked: Boolean(row.value),
            disabled: Boolean(row.disabled),
          },
          disabled: row.disabled,
          onPress: () => row.onChange(!row.value),
        },
        h(Text, null, row.label),
        row.subtitle ? h(Text, null, row.subtitle) : null,
      )
    case 'picker':
      return exports.NativePicker({
        key,
        label: row.label,
        value: row.value,
        disabled: row.disabled,
        testID: row.testID ?? row.id,
        options: row.groups.flatMap((group) => group.options),
        onChange: row.onChange,
      })
    case 'stepper':
      return h(
        View,
        { key, testID: row.testID ?? row.id },
        h(Text, null, `${row.label}: ${row.format(row.value)}`),
        h(exports.Button, {
          title: `Decrease ${row.label}`,
          disabled: row.disabled || row.value <= row.min,
          onPress: () => row.onChange(Math.max(row.min, row.value - row.step)),
        }),
        h(exports.Button, {
          title: `Increase ${row.label}`,
          disabled: row.disabled || row.value >= row.max,
          onPress: () => row.onChange(Math.min(row.max, row.value + row.step)),
        }),
      )
    case 'value':
      return h(Text, { key }, `${row.label}: ${row.value}`)
    case 'gauge':
      return h(Text, { key }, `${row.label}: ${row.valueLabel}`)
    case 'button':
      return h(exports.Button, {
        key,
        title: row.title,
        testID: row.testID ?? row.id,
        disabled: row.disabled,
        busy: row.busy,
        onPress: row.onPress,
      })
    case 'note':
      return h(Text, { key }, row.text)
    default:
      return null
  }
}

exports.NativeForm = ({ sections, empty, testID }) =>
  h(
    View,
    { testID },
    ...(!sections.length && empty
      ? [
          h(Text, { key: 'empty-title' }, empty.title),
          empty.description
            ? h(Text, { key: 'empty-description' }, empty.description)
            : null,
        ]
      : sections.map((section) =>
          h(
            View,
            { key: section.id, testID: `form-section-${section.id}` },
            section.title ? h(Text, null, section.title) : null,
            ...section.rows.map(formRow),
            section.footer ? h(Text, null, section.footer) : null,
          ),
        )),
  )
exports.ToastSurface = ({ children }) => h(View, null, children)
