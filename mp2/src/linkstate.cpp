#include <cstdio>
#include <map>
#include <vector>
#include <fstream>
#include <sstream>
#include <set>
#include <climits>
using namespace std;

void parseTopology(const char *filename, map<int, map<int, int>> &adj)
{
    ifstream fin(filename);
    int u, v, cost;
    while (fin >> u >> v >> cost)
    {
        adj[u][v] = cost;
        adj[v][u] = cost;
    }
}

void parseMessages(const char *filename, vector<tuple<int, int, string>> &messages)
{
    ifstream fin(filename);
    string line;
    while (getline(fin, line))
    {
        // create stream from string
        istringstream iss(line);
        int src, dest;
        iss >> src >> dest;
        string msg;
        getline(iss, msg);
        // remove the leading whitespace because getline will read the message with the space
        if (!msg.empty() && msg[0] == ' ')
            msg = msg.substr(1);
        messages.push_back({src, dest, msg});
    }
}

void parseChanges(const char *filename, vector<tuple<int, int, int>> &changes)
{
    ifstream fin(filename);
    int u, v, cost;
    while (fin >> u >> v >> cost)
    {
        changes.push_back({u, v, cost});
    }
}

map<int, pair<int, int>> dijkstra(int src, map<int, map<int, int>> &adj)
{
    map<int, int> dist; // shortest distance from src to each node
    map<int, int> prev; // previous node on shortest path

    for (auto &[node, neighbors] : adj)
    {
        dist[node] = INT_MAX;
    }

    dist[src] = 0;

    // set of cost and node, acts like a min-priority queue
    // set instead of pq because it is sorted and lets us remove old entries
    set<pair<int, int>> pq;
    pq.insert({0, src});

    while (!pq.empty())
    {
        auto [d, u] = *pq.begin();
        pq.erase(pq.begin()); // remove node with smallest cost

        // A node can be inserted into pq multiple times with different costs.
        // If we already processed a cheaper path to u, this entry is stale — skip it.
        if (d > dist[u])
            continue;

        for (auto &[v, weight] : adj[u])
        {
            int newDist = dist[u] + weight;
            if (newDist < dist[v])
            {
                dist[v] = newDist;
                prev[v] = u;
                pq.insert({newDist, v});
            }
            // tie-breaking
            else if (newDist == dist[v] && u < prev[v])
            {
                prev[v] = u;
            }
        }
    }

    map<int, pair<int, int>> ftable; // dest -> (nexthop, cost)
    ftable[src] = {src, 0};

    for (auto &[dest, cost] : dist)
    {
        if (dest == src || cost == INT_MAX)
            continue;

        int node = dest;
        while (prev[node] != src)
        {
            node = prev[node];
        }
        ftable[dest] = {node, cost};
    }

    return ftable;
}

void outputRound(map<int, map<int, int>> &adj, vector<tuple<int, int, string>> &messages, ofstream &fout)
{
    map<int, map<int, pair<int, int>>> allTables;
    for (auto &[node, neighbors] : adj)
    {
        allTables[node] = dijkstra(node, adj);
    }

    // print all routing tables (destination nexthope totalcost)
    for (auto &[node, table] : allTables)
    {
        for (auto &[dest, entry] : table)
        {
            fout << dest << " " << entry.first << " " << entry.second << "\n";
        }
    }

    for (auto &[src, dest, msg] : messages)
    {
        // If dest isn't in src's forwarding table, no path exists (e.g. disconnected graph)
        if (allTables[src].find(dest) == allTables[src].end())
        {
            fout << "from " << src << " to " << dest << " cost infinite hops unreachable message " << msg << "\n";
            continue;
        }

        int cost = allTables[src][dest].second;
        // Trace the path hop by hop
        vector<int> hops;
        int current = src;
        while (current != dest)
        {
            hops.push_back(current);
            current = allTables[current][dest].first;
        }

        fout << "from " << src << " to " << dest << " cost " << cost << " hops ";
        for (int h : hops)
        {
            fout << h << " ";
        }
        fout << "message " << msg << "\n";
    }
}

int main(int argc, char **argv)
{
    // printf("Number of arguments: %d", argc);
    if (argc != 4)
    {
        printf("Usage: ./linkstate topofile messagefile changesfile\n");
        return -1;
    }

    map<int, map<int, int>> adj;
    vector<tuple<int, int, string>> messages;
    vector<tuple<int, int, int>> changes;

    parseTopology(argv[1], adj);
    parseMessages(argv[2], messages);
    parseChanges(argv[3], changes);

    ofstream fout("output.txt");

    outputRound(adj, messages, fout);

    for (auto &[u, v, cost] : changes)
    {
        if (cost == -999)
        {
            adj[u].erase(v);
            adj[v].erase(u);
        }
        else
        {
            adj[u][v] = cost;
            adj[v][u] = cost;
        }
        outputRound(adj, messages, fout);
    }

    fout.close();

    return 0;
}
