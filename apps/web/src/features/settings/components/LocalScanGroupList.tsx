import React from 'react';
import type { LocalAgentCandidate } from '@/lib/api/types.temp';
import { LocalScanCandidateCard } from './LocalScanCandidateCard';
import { Button } from '@/components/ui/button';

export interface CandidateGroup {
  id: string;
  title: string;
  icon: React.ComponentType<{ className?: string }>;
  candidates: LocalAgentCandidate[];
}

interface LocalScanGroupListProps {
  groups: CandidateGroup[];
  selectedIds: Set<string>;
  onToggle: (id: string) => void;
  onToggleGroup: (ids: string[], select: boolean) => void;
}

export function LocalScanGroupList({
  groups,
  selectedIds,
  onToggle,
  onToggleGroup,
}: LocalScanGroupListProps) {
  const visibleGroups = groups.filter((g) => g.candidates.length > 0);

  return (
    <div className="space-y-5">
      {visibleGroups.map((group) => {
        const IconComponent = group.icon;
        const availableCandidates = group.candidates.filter((c) => !c.already_imported);
        const importedCount = group.candidates.length - availableCandidates.length;
        const allAvailableSelected =
          availableCandidates.length > 0 &&
          availableCandidates.every((c) => selectedIds.has(c.suggested_id));

        return (
          <div key={group.id} className="space-y-2.5">
            {/* Group Header */}
            <div className="flex items-center justify-between pb-1 border-b border-border/40">
              <div className="flex items-center gap-2">
                <IconComponent className="h-4 w-4 text-primary" />
                <h4 className="text-xs font-semibold text-foreground tracking-tight">
                  {group.title}
                </h4>
                <div className="flex items-center gap-1 text-[11px] text-muted-foreground font-mono">
                  <span className="rounded-full bg-muted px-1.5 py-0.2">
                    {group.candidates.length} 项
                  </span>
                  {importedCount > 0 && (
                    <span className="text-muted-foreground">
                      ({importedCount} 项已导入)
                    </span>
                  )}
                </div>
              </div>

              {availableCandidates.length > 0 && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() =>
                    onToggleGroup(
                      availableCandidates.map((c) => c.suggested_id),
                      !allAvailableSelected
                    )
                  }
                  className="h-6 px-2 text-[11px] text-muted-foreground hover:text-foreground"
                >
                  {allAvailableSelected ? '取消本组全选' : '全选本组'}
                </Button>
              )}
            </div>

            {/* Candidate Cards */}
            <div className="grid grid-cols-1 gap-2">
              {group.candidates.map((candidate) => (
                <LocalScanCandidateCard
                  key={candidate.suggested_id}
                  candidate={candidate}
                  isSelected={selectedIds.has(candidate.suggested_id)}
                  onToggle={onToggle}
                />
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}
