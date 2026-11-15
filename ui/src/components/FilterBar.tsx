import { SlidersHorizontalIcon } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "../lib/utils";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card } from "./ui/card";
import { Popover, PopoverContent, PopoverTrigger } from "./ui/popover";

export interface MoreFilters {
  /** How many of the hidden filters are set; shown on the button. */
  active: number;
  onClear: () => void;
  children: ReactNode;
}

/**
 * The filter bar of the Activity pages (Usage, Requests, Audit log): the common filters on one row of a
 * wide screen, the rarer ones under More filters. `className` sets the wide-screen columns.
 */
export function FilterBar({
  className,
  children,
  more,
  testId,
}: {
  className: string;
  children: ReactNode;
  more?: MoreFilters;
  testId?: string;
}) {
  return (
    <Card className={cn("mb-4 grid grid-cols-2 items-end gap-2 p-3 shadow-none sm:grid-cols-3", className)} data-testid={testId}>
      {children}
      {more && (
        <Popover>
          <PopoverTrigger asChild>
            <Button variant="outline" className="col-span-2 sm:col-span-1">
              <SlidersHorizontalIcon aria-hidden="true" />
              More filters
              {more.active > 0 && <Badge variant="secondary">{more.active}</Badge>}
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" className="grid w-80 gap-3">
            {more.children}
            <Button variant="ghost" size="sm" onClick={more.onClear} disabled={more.active === 0}>
              Clear these filters
            </Button>
          </PopoverContent>
        </Popover>
      )}
    </Card>
  );
}
