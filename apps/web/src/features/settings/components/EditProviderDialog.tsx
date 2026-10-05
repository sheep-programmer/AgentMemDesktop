import React from 'react';
import type { ProviderItem } from '@/lib/api/types.temp';
import { ProviderDialog } from './ProviderDialog';

interface EditProviderDialogProps {
  isOpen: boolean;
  onClose: () => void;
  provider: ProviderItem | null;
  onUpdate: (provider: ProviderItem) => void;
}

export function EditProviderDialog({
  isOpen,
  onClose,
  provider,
  onUpdate,
}: EditProviderDialogProps) {
  return (
    <ProviderDialog
      isOpen={isOpen}
      onClose={onClose}
      mode="edit"
      initialData={provider}
      onSubmitSuccess={onUpdate}
    />
  );
}
