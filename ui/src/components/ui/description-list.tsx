import * as React from "react";

import { cn } from "@/lib/utils";

/* Term/value pairs: a label column beside a value column, stacked on narrow screens. */

function DescriptionList({ className, ...props }: React.ComponentProps<"dl">) {
  return <dl data-slot="description-list" className={cn("m-0 grid grid-cols-1 gap-x-4 gap-y-2 text-sm sm:grid-cols-[120px_1fr]", className)} {...props} />;
}

function DescriptionTerm({ className, ...props }: React.ComponentProps<"dt">) {
  return <dt data-slot="description-term" className={cn("text-muted-foreground", className)} {...props} />;
}

function DescriptionDetails({ className, ...props }: React.ComponentProps<"dd">) {
  return <dd data-slot="description-details" className={cn("m-0 mb-2 sm:mb-0", className)} {...props} />;
}

export { DescriptionDetails, DescriptionList, DescriptionTerm };
