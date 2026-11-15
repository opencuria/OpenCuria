// Metro selects the platform implementation; TypeScript uses the iOS contract.
export { Button, IconButton, NativeAction } from "./controls.ios";
export type {
  ButtonProps,
  IconButtonProps,
  NativeActionProps,
} from "./controls.types";
