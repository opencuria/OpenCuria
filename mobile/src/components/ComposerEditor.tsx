import React from "react";
import { TextInput, type TextInputProps } from "react-native";

/**
 * UITextView (iOS) / EditText (Android), matching t3code's editor primitives.
 * Native selection, dictation, undo, spell checking and marked IME text remain
 * with the OS. React controls the text; only explicit insertions move the caret.
 */
export const ComposerEditor = React.forwardRef<TextInput, TextInputProps>(
  function ComposerEditor(props, ref) {
    return (
      <TextInput
        ref={ref}
        multiline
        submitBehavior="newline"
        textAlignVertical="top"
        autoCorrect
        spellCheck
        selectionColor={props.selectionColor}
        {...props}
      />
    );
  },
);
