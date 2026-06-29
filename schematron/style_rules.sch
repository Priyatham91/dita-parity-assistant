<?xml version="1.0" encoding="UTF-8"?>
<!--
  Style Rules — a demo Schematron rule set illustrating the validation
  layer of the parity tool.

  This file is a generic example. In a real deployment, a writing-style
  team would maintain a far larger rule set encoding their own house
  voice (e.g. preferred terminology, capitalization, citation format).
  The tool reads any `*.sch` file in this directory and surfaces every
  rule violation as a "Style/structure issues" card in the report.

  Each rule has:
    - a unique @id
    - a `<sch:context>` xpath selector
    - one or more `<sch:report>` (warning) or `<sch:assert>` (error)
      tests
    - optional `<sqf:fix>` quick-fix suggestions

  ISO-Schematron compatibility: each rule's xpath uses XPath 1 only,
  to match what `lxml.isoschematron` can compile. Multi-line predicates
  and XPath 2 functions are intentionally avoided.
-->
<sch:schema xmlns:sch="http://purl.oclc.org/dsdl/schematron"
            xmlns:sqf="http://www.schematron-quickfix.com/validator/process"
            queryBinding="xslt">

  <sch:title>DITA style and structure rules</sch:title>
  <sch:ns prefix="sqf" uri="http://www.schematron-quickfix.com/validator/process"/>

  <!-- ============================================================== -->
  <!-- Title rules                                                    -->
  <!-- ============================================================== -->

  <sch:pattern id="titles">
    <sch:rule context="title">

      <sch:report id="SR_001" test="contains(., '  ')" role="warning">
        SR_001: Title contains double spaces.
      </sch:report>

      <sch:report id="SR_002" test="substring(., string-length(.)) = '.'" role="error" sqf:fix="strip_period">
        SR_002: Titles should not end with a period.
        <sqf:fix id="strip_period">
          <sqf:description><sqf:title>Strip trailing period.</sqf:title></sqf:description>
          <sqf:stringReplace regex="\.$" select="''"/>
        </sqf:fix>
      </sch:report>

      <sch:report id="SR_003" test="contains(., '&#8212;')" role="warning">
        SR_003: Avoid em dashes (—) in titles. Prefer commas or rephrasing.
      </sch:report>

    </sch:rule>
  </sch:pattern>

  <!-- ============================================================== -->
  <!-- Note element rules                                             -->
  <!-- ============================================================== -->

  <sch:pattern id="notes">
    <sch:rule context="note">

      <sch:assert id="SR_010" test="@type" role="error">
        SR_010: Every &lt;note&gt; must have a @type attribute (e.g. type="important", type="tip").
      </sch:assert>

      <sch:assert id="SR_011" test="p or ul or ol" role="error">
        SR_011: A &lt;note&gt; must contain a block element (&lt;p&gt;, &lt;ul&gt;, or &lt;ol&gt;) — never bare text.
      </sch:assert>

      <sch:report id="SR_012" test="count(p) &gt; 4" role="warning">
        SR_012: Notes with more than 4 paragraphs are hard to scan. Consider splitting into a separate concept.
      </sch:report>

    </sch:rule>
  </sch:pattern>

  <!-- ============================================================== -->
  <!-- Topic-level rules                                              -->
  <!-- ============================================================== -->

  <sch:pattern id="topic-level">
    <sch:rule context="concept | task | reference">

      <sch:report id="SR_020" test="not(shortdesc)" role="warning">
        SR_020: A short description is strongly recommended at the topic level — it surfaces in search results.
      </sch:report>

      <sch:assert id="SR_021" test="@id" role="error">
        SR_021: Every topic must have an @id attribute for cross-referencing.
      </sch:assert>

    </sch:rule>
  </sch:pattern>

  <!-- ============================================================== -->
  <!-- Inline markup hygiene                                          -->
  <!-- ============================================================== -->

  <sch:pattern id="inline-markup">
    <sch:rule context="em">

      <sch:report id="SR_030" test="parent::li and not(parent::li/p)" role="warning">
        SR_030: Inline &lt;em&gt; as a direct child of &lt;li&gt; may be rejected by some authoring environments. Wrap in &lt;p&gt; if the &lt;li&gt; contains block content.
      </sch:report>

      <sch:report id="SR_031" test="contains(., ':') and string-length(.) &lt; 30" role="warning">
        SR_031: Short emphasized phrases ending in a colon are often UI labels. Consider &lt;uicontrol&gt; instead of &lt;em&gt;.
      </sch:report>

    </sch:rule>

    <sch:rule context="uicontrol">

      <sch:assert id="SR_035" test="string-length(.) &gt; 0" role="error">
        SR_035: A &lt;uicontrol&gt; element must not be empty.
      </sch:assert>

    </sch:rule>
  </sch:pattern>

  <!-- ============================================================== -->
  <!-- Whitespace and typography                                      -->
  <!-- ============================================================== -->

  <sch:pattern id="whitespace">
    <sch:rule context="text()">

      <sch:report id="SR_040" test="contains(., '  ')" role="warning">
        SR_040: Double space detected. Single spaces only between sentences.
      </sch:report>

      <sch:report id="SR_041" test="contains(., ' .') or contains(., ' ,')" role="error">
        SR_041: Space before punctuation. Punctuation should hug the preceding word.
      </sch:report>

    </sch:rule>
  </sch:pattern>

  <!-- ============================================================== -->
  <!-- Table rules                                                    -->
  <!-- ============================================================== -->

  <sch:pattern id="tables">

    <sch:rule context="table">
      <sch:assert id="SR_050" test="title" role="error">
        SR_050: Tables must have a &lt;title&gt; element so screen readers can announce them.
      </sch:assert>
    </sch:rule>

    <sch:rule context="entry">
      <sch:report id="SR_051" test="not(normalize-space(.))" role="warning">
        SR_051: Empty table cell. Use "—" or "N/A" instead of leaving it blank.
      </sch:report>
    </sch:rule>

  </sch:pattern>

  <!-- ============================================================== -->
  <!-- Link rules                                                     -->
  <!-- ============================================================== -->

  <sch:pattern id="links">
    <sch:rule context="xref">

      <sch:assert id="SR_060" test="@href" role="error">
        SR_060: An &lt;xref&gt; element must have an @href attribute.
      </sch:assert>

      <sch:report id="SR_061" test="contains(text(), 'click here') or contains(text(), 'here')" role="warning">
        SR_061: Avoid "click here" link text — use descriptive link text instead.
      </sch:report>

    </sch:rule>
  </sch:pattern>

</sch:schema>
