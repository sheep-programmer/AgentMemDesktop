import React from 'react';
import type { ProviderItem } from '@/lib/api/types.temp';
import { ProviderDialog } from './ProviderDialog';

interface AddProviderDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onAdd: (provider: ProviderItem) => void;
}

export function AddProviderDialog({ isOpen, onClose, onAdd }: AddProviderDialogProps) {
  return (
    <ProviderDialog
      isOpen={isOpen}
      onClose={onClose}
      mode="add"
      onSubmitSuccess={onAdd}
    />
  );
}
