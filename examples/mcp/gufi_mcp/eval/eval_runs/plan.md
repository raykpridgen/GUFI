# Evaluation Log Analysis

Each of the runs in this dir and in old contain information on how an agent performed with an MCP tool suite
The goal is to reflect through data and charts how the agent responded to different design choices of the tools
Over time, things like SQL schema, column hints, tool reworks, and other changes were made

Note that previous runs included tools that are no longer being considered. These mainly include user-facing programs like gufi_find
Both 'single' and 'general' evaluation can be used for analysis, but the times when the agent uses user-facing tools should be discounted

These graphs should answer questions like:
- How did a new iteration improve the agent's ability to return a correct response? Or match the ground truth rows?
- Which prompts gave agents the most difficulty?


In the plots folder in this directory, there are previous generated graphs of metrics. These graphs can be similar, but must span each iteration
This way consistent trends can be established on what worked and what didn't for the MCP server.
Please place the plots in a folder called 'plots' in this directory.
