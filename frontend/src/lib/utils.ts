import { clsx } from "clsx"
export function cn(...a: any[]) { return clsx(a) }
export const API = (path: string) => path.startsWith("/api") ? path : `/api${path}`
