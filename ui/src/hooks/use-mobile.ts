import * as React from "react"

// Tailwind's md, in the same unit: a browser font size other than 16px moves both together.
const DESKTOP_QUERY = "(min-width: 48rem)"

export function useIsMobile() {
  const [isMobile, setIsMobile] = React.useState<boolean | undefined>(undefined)

  React.useEffect(() => {
    const mql = window.matchMedia(DESKTOP_QUERY)
    const onChange = () => {
      setIsMobile(!mql.matches)
    }
    mql.addEventListener("change", onChange)
    onChange()
    return () => mql.removeEventListener("change", onChange)
  }, [])

  return !!isMobile
}
