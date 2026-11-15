/** The account control that opens Settings; shows the user's initials. */
export interface AccountButtonProps {
  initials: string;
  /** Spoken name, e.g. "Settings, Ada Lovelace". */
  label: string;
  onPress: () => void;
  testID?: string;
}

/** The sidebar's prominent New chat control. */
export interface NewChatButtonProps {
  onPress: () => void;
  disabled?: boolean;
  testID?: string;
}

/** Round icon control in the sidebar header, e.g. Search. */
export interface SidebarIconButtonProps {
  symbol: "magnifyingglass";
  label: string;
  onPress: () => void;
  testID?: string;
}

/** The system search field that filters the sidebar. */
export interface SidebarSearchFieldProps {
  value: string;
  onChange: (value: string) => void;
  /** Ends the search: clears the query and closes the keyboard. */
  onCancel: () => void;
  placeholder: string;
  accessibilityLabel: string;
  testID?: string;
}
