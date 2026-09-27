import { useQuery } from "@tanstack/react-query";
import { request } from "./api";
import type { AwsAccount, Connection } from "./types";

export function useAccounts() {
  return useQuery({
    queryKey: ["aws-accounts"],
    queryFn: () => request<{ accounts: AwsAccount[] }>("/api/aws-accounts").then((r) => r.accounts),
  });
}

export function useConnections() {
  return useQuery({
    queryKey: ["integrations"],
    queryFn: () => request<{ connections: Connection[] }>("/api/integrations").then((r) => r.connections),
  });
}
