Updated: 07/24/2019

# Overview

This shows how to use Archy JSON schema files with the Red Hat YAML extension in Microsoft Visual Studio Code.

## Setup

  1.  Open Visual Studio Code.

  2.  Menu:  File | Open - Open up the Archy home directory:

        ~/archyHome
         
  3.  Menu:  View | Extensions - Enable the YAML extension made by Red Hat.
     
  4.  Configure the YAML extension.  You can do this by selecting Configure Extension Settings
      on the YAML extension and editing the Yaml:Schemas setting or by opening Visual Studio Code's
      settings.json file.  Add the 'yaml.schemas' entry from the 'settings.json' file in this
      directory. What this does is tell the YAML extension to use the JSON schemas that were generated
      in the schemas directory as part of Archy's setup so that any yaml file whose name ends with 'Flow'
      will use the Archy flow yaml format and any yaml file whose name starts with 'shared' will use the
      Archy common yaml format.

## Usage

You can then open up Archy example flow yaml files and start watching the YAML plugin use the Archy
JSON schemas for validation.  Or if you create a new 'myFlow.yaml' file, press control-space and you
should see typeahead show suggestions asking you what kind of flow you want to create.

---------------------------------------------------------------------------------------------------------------
#### Configuration tested:

  > Microsoft Visual Studio Code - Ver. 1.36.1
    Copyright (C) 2019 Microsoft

  > Red Hat YAML extension       - Ver. 0.4.1
    https://github.com/redhat-developer/vscode-yaml
