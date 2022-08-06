# Overview

SQLAlchemy support:

- not "production-ready"

Django support:

- totally experimental

# Bitemporal Data Theory

## What is Bitemporal Data?

It's in the name! "Bi"-"Temporal" data incorporates two continuous dimensions of time.

What are the two dimensions of time?

One is "application" time, representing the business's view of a key's historical values.
A boundary in application time is "true" point in time that a key's value changed.

The other is "transaction" time, representing the *database's* view of a key's value.
A boundary in transaction time is the point that the database's opinion of a key's
application history changed.

## An Example

For example, let's say you move from 123 Apple St to 456 Banana St. The day of your
move is June 13th. Once you're all settled in, you call the bank on June 20th, to let
them know that you moved on June 13th.

The boundary in *application* history for your move is June 13th.

The break in the bank's *transaction* history is June 20th, because that is the day that
the bank's perspective on your address history changed.

Below is a snapshot of the bank's `address` table, from June 19th:

```
txn_period      , app_period      , user_id, address 
"[2022-01-01, )", "[2022-01-01, )", 42     , 123 Apple St    
```

Notice the lower `txn_period` and `app_period` values are 2022-01-01. This is presumably
the day you signed up with the bank, explaining the `txn_period`.

The `app_period` value is explained by the fact that they don't have any info about your
address prior to `2022-01-01`. If they did (e.g. if they pulled your credit history),
this value could be earlier.

Here's another snapshot of `address`, from June 21st:

```
txn_period      , app_period                , user_id, address 
"[2022-06-20, )", "[2022-01-01, 2022-06-13)", 42     , 123 Apple St    
"[2022-06-20, )", "[2022-06-13, )"          , 42     , 456 Banana St
```

As we can see, the earlier `app_period` has its app period "closed" to the provided
date of the move, June 13th. The fact that this information was processed on June 20th
is recorded by the database in the `txn_period`.

## Non-Destructive Writes

This is great! But what about the information that predates 6/20? We lost our view of
the world on June 19th when we updated it on June 20th. That stinks!

Except we didn't! There's a corresponding table, `address_history`, that persists all
records with a *terminated transaction period*.

On June 19th, there was no record for user `42`, since they had no mutation history.

On June 21st, `address_history` would look like this:

```
txn_period                , app_period      , user_id, address 
"[2022-01-01, 2022-06-20)", "[2022-01-01, )", 42     , 123 Apple St    
```

This means that the union of `address` and `address_history` has a pretty special
property: non-destructive updates! It has non-destructive deletes, too, which together
makes up the title of this section: non-destructive "writes", or mutations.

This is a nice property to have!

## Meta Superpowers

`app_period` means you the variation of a value over time for a key.

`txn_period` and the `_history` tables mean you also know how your opinion of this
value timeline *changed over time*.

This is a superpower!

Let's drop back into the bank example.

On July 1st you call complain that you never received their checkbook that was supposed
to come in the mail on June 15th. What gives!

A support person who only had view of the `app_period` would know that the
customer moved on 6/13, but wouldn't know that the customer updated the address on 6/20.

A support person who only had view of `txn_period` would know that they updated their
address on 6/20, but wouldn't know that the customer *actually* moved on 6/13.

A support person with access to *both* has a clearer picture. They can see that the
address timeline changed on 6/20, and that they moved on 6/13. They also see the previous
address, and can know where the checkbook probably went to.

A support person with access to the full `address` table and `address_history` table
can understand what the *bank* believed on 6/14, when the checkbook was mailed out from
the local bank branch, by looking for the records in `address_history` whose `txn_period`
overlaps with `2022-06-14`.

They could even re-run the program that generated the shipping label with a fixed
transaction time of 06/14, and validate that it would generate a label for 123 Apple St.

In other words, it is a super powerful debugging and auditing tool!

# Bitemporal Data in Practice

OK, we understand the motivation! How do we implement it?

This project aims to implement the heavy lifting of bitemporal data *in the database*.

It also offers some bindings for interfacing with bitemporal data in Python with
SQLAlchemy and Django, so that it's easier to define and interface with. 

## SQL Functions

The main function is `record_txn_history`, which does the heavy lifting of "expiring"
outdated records into the `_history` table.

It's a trigger that's meant to execute when a table changes.

So, for every bitemporal model, this function needs to be declared as a trigger that
executes when the root table changes. The SQLAlchemy and Django libs instantiate this
trigger as part of the model definition.

There are two other functions that are helpers for manipulating `record_txn_history`.
One forces the observed "wall clock" to some constant value, while another allows
the application to temporarily disable history-tracking within the scope of a transaction.

A final function is a helper for splitting a single "application span" (i.e. a row in
the current table) into two distinct periods. This is used when clearing space to
insert a new record. For example, if you temporarily lived at 456 Banana St for 3 days
between 6/13 and 6/16, and wanted the bank to know about it.

Right now, the more abstract function of clearing space in application time happens in
a helper library, rather than in a SQL function. In the future, we might want to push
this into a SQL definition to make it more performant and portable to other libraries.
