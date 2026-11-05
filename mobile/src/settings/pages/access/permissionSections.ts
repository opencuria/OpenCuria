import type { ApiKeyPermission } from "@opencuria/client";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import {
  groupPermissions,
  setFullAccess,
  togglePermission,
  type PermissionForm,
} from "./apiKeys";

/** "Org credential services" from "org_credential_services". */
function groupTitle(group: string): string {
  const text = group.replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** The Full access switch and, when restricted, one toggle per permission. */
export function permissionSections(
  form: PermissionForm,
  available: ApiKeyPermission[],
  onChange: (form: PermissionForm) => void,
  options: { disabled?: boolean; footer?: string } = {},
): NativeFormSection[] {
  return [
    {
      id: "access",
      title: "Permissions",
      footer: form.full
        ? "This key can access all operations. Turn off to restrict it."
        : (options.footer ?? "This key can only use the selected operations."),
      rows: [
        {
          kind: "toggle",
          id: "full-access",
          label: "Full access",
          value: form.full,
          disabled: options.disabled,
          onChange: (full) => onChange(setFullAccess(form, full, available)),
        },
      ],
    },
    ...(form.full
      ? []
      : groupPermissions(available).map(({ group, permissions }) => ({
          id: `group-${group}`,
          title: groupTitle(group),
          rows: permissions.map((permission) => ({
            kind: "toggle" as const,
            id: `permission-${permission.value}`,
            label: permission.value,
            subtitle: permission.description,
            value: form.selected.includes(permission.value),
            disabled:
              options.disabled ||
              (form.selected.length === 1 &&
                form.selected[0] === permission.value),
            onChange: (on: boolean) =>
              onChange(togglePermission(form, permission.value, on)),
          })),
        }))),
  ];
}
